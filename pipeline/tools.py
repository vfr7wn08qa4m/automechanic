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
import html as _html
import json
import re as _re
import sys
from pathlib import Path

from . import config
from .ado import AdoClient
from .case_schema import RepairCase
from .store import append_jsonl, archive_blob


def _material_from_body(wi: dict) -> str:
    """Материал (транскрипт видео / текст форум-треда) из ТЕЛА тикета: всё после
    первого <hr>, без уже дописанного RepairCase. Единый источник и для видео,
    и для форумов — Claude-агенту не нужно ничего до-фетчить."""
    desc = (wi["fields"].get("System.Description", "") or "").split("<hr><b>RepairCase", 1)[0]
    if "<hr>" in desc:
        desc = desc.split("<hr>", 1)[1]      # отбрасываем метаданные до первого <hr>
    txt = _html.unescape(_re.sub(r"<[^>]+>", " ", desc))
    return _re.sub(r"\s+", " ", txt).strip()


def _material_from_r2(vid: str) -> str:
    """Фолбэк для старых видео-тикетов: транскрипт из R2-архива."""
    try:
        from .store import s3_client
        from .subtitle_providers import lines_from_raw
        from .subtitles import to_prompt_text
        listed = s3_client().list_objects_v2(Bucket=config.S3_BUCKET, Prefix=f"subs/{vid}.")
        for obj in listed.get("Contents", []):
            key = obj["Key"]
            raw = s3_client().get_object(Bucket=config.S3_BUCKET, Key=key)["Body"].read()
            return to_prompt_text(lines_from_raw(key.rsplit(".", 1)[-1],
                                                 raw.decode("utf-8", "replace")))
    except Exception:  # noqa: BLE001
        return ""
    return ""


def cmd_next_subs(batch: int, partition: str | None) -> None:
    # Отдаём ТОЛЬКО метаданные (без транскрипта) — под-агент берёт материал сам через
    # get-material. Так батч можно делать большим (100+), не переполняя контекст
    # главного агента полными транскриптами (~18k симв на видео). Клейм — при
    # get-material (claim-at-process), поэтому необработанные за прогон остаются в
    # очереди для следующего (ничего не застревает «занятым»).
    ado = AdoClient()
    ids = ado.query_by_state("subs", top=batch, partition=partition)
    out = []
    for wi in ado.get_batch(ids, fields=("System.Title",)):
        title = wi["fields"].get("System.Title", "")
        vid = ado.video_id_from_title(title) or ""
        stype = "forum" if vid.startswith("frm-") else "youtube"
        out.append({"wi_id": wi["id"], "video_id": vid,
                    "source_type": stype, "title": title.split("]", 1)[-1].strip()})
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)


def cmd_get_material(wi_id: int) -> None:
    """Материал ОДНОГО тикета (транскрипт видео / текст форум-треда) для под-агента.
    Клеймит айтем (claim-at-process): занят -> skip; нет материала -> fail+пометка."""
    ado = AdoClient()
    if not ado.claim(wi_id, "claude-cloud"):
        json.dump({"wi_id": wi_id, "skip": "занят другим воркером"},
                  sys.stdout, ensure_ascii=False)
        return
    wi = ado.get(wi_id)
    title = wi["fields"]["System.Title"]
    vid = ado.video_id_from_title(title) or ""
    url = (AdoClient.source_url(wi) or f"https://www.youtube.com/watch?v={vid}")
    material = _material_from_body(wi)
    if not material and config.S3_ENDPOINT and not vid.startswith("frm-"):
        material = _material_from_r2(vid)
    if not material:
        ado.set_state(wi_id, "failed",
                      comment="нет материала в теле тикета (subs-fetch/краул не отработал?)")
        json.dump({"wi_id": wi_id, "no_material": True}, sys.stdout, ensure_ascii=False)
        return
    stype = ("forum" if vid.startswith("frm-")
             else ("carcarekiosk" if "carcarekiosk.com" in url else "youtube"))
    json.dump({"wi_id": wi_id, "video_id": vid, "url": url, "source_type": stype,
               "title": title.split("]", 1)[-1].strip(), "lang": "", "transcript": material},
              sys.stdout, ensure_ascii=False, indent=1)


def cmd_save_case(wi_id: int, case_file: str) -> None:
    case = RepairCase.model_validate_json(
        Path(case_file).read_text(encoding="utf-8"))
    case.distill_model = case.distill_model or "claude-cloud"
    append_jsonl(case)
    vid = case.source.video_id or f"wi-{wi_id}"
    key = archive_blob(f"cases/{vid}.json", case.model_dump_json())
    ado = AdoClient()
    # кейс -> НАЗАД В ТЕЛО ТИКЕТА (ADO = база, «материал/результат в воркайтем»)
    import html as _h
    ado.append_description(wi_id,
        f"<hr><b>RepairCase</b> (system: {_h.escape(case.system or '')}, "
        f"conf {case.confidence}) <pre>{_h.escape(case.model_dump_json())}</pre>")
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
    p1.add_argument("--batch", type=int, default=100)
    p1.add_argument("--partition", choices=["even", "odd"], default=None)

    p5 = sub.add_parser("get-material")
    p5.add_argument("wi_id", type=int)

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
    elif args.cmd == "get-material":
        cmd_get_material(args.wi_id)
    elif args.cmd == "save-case":
        cmd_save_case(args.wi_id, args.case_file)
    elif args.cmd == "fail":
        cmd_fail(args.wi_id, args.reason)
    elif args.cmd == "reparse-channel":
        cmd_reparse_channel(args.channel_id)


if __name__ == "__main__":
    main()
