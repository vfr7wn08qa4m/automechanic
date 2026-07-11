"""CLI-инструменты для облачного Claude-агента дистилляции (подписка юзера).

Агент в Claude Code cloud по расписанию делает:
    python -m pipeline.tools next-subs --batch 5   # забрать+заклеймить айтемы,
                                                   # получить транскрипты JSON'ом
    ...сам пишет кейс по схеме (см. claude/DISTILL_AGENT.md)...
    python -m pipeline.tools save-case 123 case.json   # валидация+архив+state
    python -m pipeline.tools fail 123 "причина"        # если не вышло

Никаких LLM-ключей не нужно: «сильная модель» — сам Claude в сессии.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config
from .ado import AdoClient
from .case_schema import RepairCase
from .store import append_jsonl, archive_blob


def cmd_next_subs(batch: int, partition: str | None) -> None:
    ado = AdoClient()
    ids = ado.query_by_state("subs", top=batch, partition=partition)
    out = []
    for wi_id in ids:
        if not ado.claim(wi_id, "claude-cloud"):
            continue
        wi = ado.get(wi_id)
        title = wi["fields"]["System.Title"]
        vid = ado.video_id_from_title(title) or ""
        url = (AdoClient.source_url(wi)
               or f"https://www.youtube.com/watch?v={vid}")
        try:
            # транскрипт: из R2-архива, иначе заново (роутер по источнику)
            from .subtitle_providers import lines_from_raw, transcript_for_item
            lang, lines = "", []
            if config.S3_ENDPOINT:
                from .store import s3_client
                listed = s3_client().list_objects_v2(
                    Bucket=config.S3_BUCKET, Prefix=f"subs/{vid}.")
                for obj in listed.get("Contents", []):
                    key = obj["Key"]
                    parts = key.rsplit(".", 2)
                    lang, ext = parts[-2], parts[-1]
                    raw = s3_client().get_object(
                        Bucket=config.S3_BUCKET, Key=key)["Body"].read()
                    lines = lines_from_raw(ext, raw.decode("utf-8", errors="replace"))
                    break
            if not lines:
                tr = transcript_for_item(url, vid)
                lang, lines = tr.lang, tr.lines
            from .subtitles import to_prompt_text
            out.append({
                "wi_id": wi_id, "video_id": vid,
                "url": url,
                "source_type": ("carcarekiosk" if "carcarekiosk.com" in url
                                else "youtube"),
                "title": title.split("]", 1)[-1].strip(),
                "lang": lang,
                "transcript": to_prompt_text(lines),
            })
        except Exception as e:  # noqa: BLE001
            ado.set_state(wi_id, "failed", comment=f"transcript error: {e}")
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)


def cmd_save_case(wi_id: int, case_file: str) -> None:
    case = RepairCase.model_validate_json(
        Path(case_file).read_text(encoding="utf-8"))
    case.distill_model = case.distill_model or "claude-cloud"
    append_jsonl(case)
    vid = case.source.video_id or f"wi-{wi_id}"
    key = archive_blob(f"cases/{vid}.json", case.model_dump_json())
    ado = AdoClient()
    state = "distilled" if not case.off_topic else "offtopic"
    ado.set_state(wi_id, state,
                  comment=f"case: {case.system} | {case.problem_summary[:120]}",
                  link=f"s3://{config.S3_BUCKET}/{key}" if key else "")
    print(f"#{wi_id}: {state}")


def cmd_fail(wi_id: int, reason: str) -> None:
    AdoClient().set_state(wi_id, "failed", comment=reason[:300])
    print(f"#{wi_id}: failed")


def cmd_reparse_channel(channel_id: str) -> None:
    """Переизвлечь все видео канала по новой схеме: дети ВСЕХ чанков -> state:subs
    (транскрипты уже в R2, дистилляция прогонит их заново, теперь с правилами)."""
    ado = AdoClient()
    kind = next((k for k in ("channel", "site", "forum")
                 if ado.channel_shards(channel_id, k)), None)
    if not kind:
        print(f"эпик канала {channel_id} не найден")
        return
    child_vids = ado.channel_all_child_video_ids(channel_id, kind)  # по всем чанкам
    n = 0
    for vid in child_vids:
        wi = ado.find_video_item(vid)
        if wi:
            ado.set_state(wi, "subs", comment="reparse: переизвлечь по новой схеме")
            n += 1
    print(f"канал {channel_id} ({kind}): {n} видео -> state:subs (переизвлечение)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("next-subs")
    p1.add_argument("--batch", type=int, default=5)
    p1.add_argument("--partition", choices=["even", "odd"], default=None)

    p2 = sub.add_parser("save-case")
    p2.add_argument("wi_id", type=int)
    p2.add_argument("case_file")

    p3 = sub.add_parser("fail")
    p3.add_argument("wi_id", type=int)
    p3.add_argument("reason")

    p4 = sub.add_parser("reparse-channel")
    p4.add_argument("channel_id")

    args = ap.parse_args()
    if args.cmd == "next-subs":
        cmd_next_subs(args.batch, args.partition)
    elif args.cmd == "save-case":
        cmd_save_case(args.wi_id, args.case_file)
    elif args.cmd == "fail":
        cmd_fail(args.wi_id, args.reason)
    elif args.cmd == "reparse-channel":
        cmd_reparse_channel(args.channel_id)


if __name__ == "__main__":
    main()
