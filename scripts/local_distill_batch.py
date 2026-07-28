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
import html as _html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config                                   # noqa: E402
from pipeline.ado import AdoClient                            # noqa: E402
from pipeline.case_schema import Source                       # noqa: E402
from pipeline.distill import distill                          # noqa: E402
from pipeline.store import append_jsonl, archive_blob        # noqa: E402
from pipeline.tools import _material_from_body               # noqa: E402


def distill_batch(ado, batch: int, partition: str | None,
                  worker: str | None = None) -> int:
    """Один батч дистилляции: state:subs -> RepairCase -> state:distilled/offtopic.

    Возвращает количество обработанных (claimed) воркайтемов.
    """
    if worker is None:
        worker = f"distill-{partition or os.getenv('CI_ACCOUNT', 'solo')}"
    ids = ado.query_by_state("subs", top=batch, partition=partition)
    print(f"work items в state:subs (partition={partition}): {len(ids)}")
    processed = 0
    for wi_id in ids:
        if not ado.claim(wi_id, worker):
            print(f"  #{wi_id}: уже занят, пропуск")
            continue
        processed += 1
        wi = ado.get(wi_id)
        title = wi["fields"]["System.Title"]
        vid = ado.video_id_from_title(title)
        is_forum = bool(vid and vid.startswith("frm-"))
        url = (AdoClient.source_url(wi)
               or f"https://www.youtube.com/watch?v={vid}")
        try:
            # Транскрипт/текст УЖЕ лежит в теле воркайтема — subs-fetch (видео) и
            # краул (форум) его туда положили. Читаем ОТТУДА и сразу в ЛЛМ, БЕЗ
            # перефетча ютуба (он и лишний, и поднимал sync-playwright -> конфликт
            # event loop с браузерным Qwen). Один источник для видео и форумов.
            transcript = _material_from_body(wi)
            if not transcript:
                ado.set_state(wi_id, "failed",
                              comment="нет транскрипта в теле тикета (subs-fetch не отработал?)")
                print(f"  #{wi_id} {vid}: нет материала в теле -> failed")
                continue
            if is_forum:
                source = Source(type="forum", url=url, video_id=vid, lang="",
                                title=title.split("]", 1)[-1].strip(),
                                channel=urlparse(url).hostname or "forum")
            else:
                src_type = "carcarekiosk" if "carcarekiosk.com" in url else "youtube"
                source = Source(type=src_type, url=url, video_id=vid, lang="",
                                title=title.split("]", 1)[-1].strip())
            case = distill(transcript, source)
            append_jsonl(case)
            key = archive_blob(f"cases/{vid}.json", case.model_dump_json())
            state = "distilled" if not case.off_topic else "offtopic"
            # РЕЗУЛЬТАТ НАЗАД В ВОРКАЙТЕМ: кейс дописывается в тело тикета
            ado.append_description(wi_id,
                f"<hr><b>RepairCase</b> (system: {_html.escape(case.system or '')}, "
                f"conf {case.confidence}) <pre>{_html.escape(case.model_dump_json())}</pre>")
            ado.set_state(wi_id, state,
                          comment=f"case: {case.system} | {case.problem_summary[:120]}",
                          link=f"s3://{config.S3_BUCKET}/{key}" if key else "")
            print(f"  #{wi_id} {vid}: {state} ({case.system})")
        except Exception as e:  # noqa: BLE001
            ado.set_state(wi_id, "failed", comment=f"distill error: {str(e)[:150]}")
            print(f"  #{wi_id} {vid}: FAIL {str(e)[:150]}")
    return processed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--partition", choices=["even", "odd", "solo"],
                    default=os.getenv("PARTITION") or None)
    args = ap.parse_args()
    if args.partition == "solo":
        args.partition = None
    worker = f"distill-{args.partition or os.getenv('CI_ACCOUNT', 'solo')}"

    # ci_budget (лимит CI-минут) — только для облачных прогонов; локально комп свой,
    # минуты не жжём, поэтому гард здесь не применяем (был рудимент от CI-версии).

    ado = AdoClient()
    distill_batch(ado, args.batch, args.partition, worker)


if __name__ == "__main__":
    main()
