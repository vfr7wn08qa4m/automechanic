"""Этап «дистилляция»: state:subs -> кейс -> архив -> state:distilled.

Дистилляция — это API-вызовы (NIM/Groq/Cerebras), датацентровый IP не мешает,
поэтому этап живёт в облачном CI вместе с остальными. Локально запускать можно,
но не обязательно. Транскрипт берётся из R2-архива, иначе — заново через
цепочку провайдеров.

Парные аккаунты: --partition even|odd (или env PARTITION), как у этапа титров.

    python scripts/local_distill_batch.py --batch 20 [--partition even|odd]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config                                   # noqa: E402
from pipeline.ado import AdoClient                            # noqa: E402
from pipeline.case_schema import Source                       # noqa: E402
from pipeline.distill import distill                          # noqa: E402
from pipeline.store import append_jsonl, archive_blob, s3_client  # noqa: E402
from pipeline.subtitle_providers import lines_from_raw, transcript_for_item  # noqa: E402
from pipeline.subtitles import to_prompt_text                 # noqa: E402


def load_lines(vid: str) -> tuple[str, list[tuple[int, str]]] | None:
    """Транскрипт из R2-архива: (lang, lines) или None."""
    if not config.S3_ENDPOINT:
        return None
    s3 = s3_client()
    listed = s3.list_objects_v2(Bucket=config.S3_BUCKET, Prefix=f"subs/{vid}.")
    for obj in listed.get("Contents", []):
        key = obj["Key"]                       # subs/{vid}.{lang}.{ext}
        parts = key.rsplit(".", 2)
        lang, ext = parts[-2], parts[-1]
        raw = s3.get_object(Bucket=config.S3_BUCKET, Key=key)["Body"].read()
        return lang, lines_from_raw(ext, raw.decode("utf-8", errors="replace"))
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--partition", choices=["even", "odd", "solo"],
                    default=os.getenv("PARTITION") or None)
    args = ap.parse_args()
    if args.partition == "solo":
        args.partition = None
    worker = f"distill-{args.partition or os.getenv('CI_ACCOUNT', 'solo')}"

    from pipeline.ci_budget import guard
    if not guard(20):     # месячный лимит минут исчерпан -> тихий выход
        return

    ado = AdoClient()
    ids = ado.query_by_state("subs", top=args.batch, partition=args.partition)
    print(f"work items в state:subs (partition={args.partition}): {len(ids)}")

    for wi_id in ids:
        if not ado.claim(wi_id, worker):
            print(f"  #{wi_id}: уже занят, пропуск")
            continue
        wi = ado.get(wi_id)
        title = wi["fields"]["System.Title"]
        vid = ado.video_id_from_title(title)
        url = (AdoClient.source_url(wi)
               or f"https://www.youtube.com/watch?v={vid}")
        try:
            got = load_lines(vid)
            if got is None:
                tr = transcript_for_item(url, vid)
                got = (tr.lang, tr.lines)
            lang, lines = got
            transcript = to_prompt_text(lines)
            src_type = "carcarekiosk" if "carcarekiosk.com" in url else "youtube"
            source = Source(type=src_type, url=url, video_id=vid, lang=lang,
                            title=title.split("]", 1)[-1].strip())
            case = distill(transcript, source)
            case.lang = case.lang or lang
            append_jsonl(case)
            key = archive_blob(f"cases/{vid}.json", case.model_dump_json())
            state = "distilled" if not case.off_topic else "offtopic"
            ado.set_state(wi_id, state,
                          comment=f"case: {case.system} | {case.problem_summary[:120]}",
                          link=f"s3://{config.S3_BUCKET}/{key}" if key else "")
            print(f"  #{wi_id} {vid}: {state} ({case.system})")
        except Exception as e:  # noqa: BLE001
            ado.set_state(wi_id, "failed", comment=f"distill error: {e}")
            print(f"  #{wi_id} {vid}: FAIL {e}")


if __name__ == "__main__":
    main()
