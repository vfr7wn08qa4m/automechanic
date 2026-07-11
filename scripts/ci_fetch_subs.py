"""CI-этап «титры»: state:new -> транскрипт -> архив -> state:subs.

Работает на ОБЛАЧНОМ CircleCI-агенте: цепочка провайдеров транскрипта
(pipeline/subtitle_providers.py) не требует чистого IP — yt-dlp быстро
фейлится в датацентре, цепочка уходит в Invidious/Supadata/прокси.

Парные аккаунты: у аккаунта A расписание по чётным минутам + PARTITION=even,
у аккаунта B по нечётным + PARTITION=odd. Партиция делит work items по
чётности id, плюс каждый айтем атомарно клеймится (rev-test) — гонок нет.

    python scripts/ci_fetch_subs.py --batch 10 [--partition even|odd]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config                                   # noqa: E402
from pipeline.ado import AdoClient                            # noqa: E402
from pipeline.store import archive_blob                       # noqa: E402
from pipeline.subtitle_providers import transcript_for_item   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--partition", choices=["even", "odd", "solo"],
                    default=os.getenv("PARTITION") or None)
    args = ap.parse_args()
    if args.partition == "solo":
        args.partition = None
    worker = f"subs-{args.partition or os.getenv('CI_ACCOUNT', 'solo')}"

    from pipeline.ci_budget import guard
    if not guard(15):     # месячный лимит минут исчерпан -> тихий выход
        return

    ado = AdoClient()
    ids = ado.query_by_state("new", top=args.batch, partition=args.partition)
    print(f"work items в state:new (partition={args.partition}): {len(ids)}")

    for wi_id in ids:
        if not ado.claim(wi_id, worker):
            print(f"  #{wi_id}: уже занят, пропуск")
            continue
        wi = ado.get(wi_id)
        vid = ado.video_id_from_title(wi["fields"]["System.Title"])
        if not vid:
            ado.set_state(wi_id, "failed", "no [vid:] marker in title")
            continue
        url = (AdoClient.source_url(wi)
               or f"https://www.youtube.com/watch?v={vid}")
        try:
            tr = transcript_for_item(url, vid)
            key = archive_blob(f"subs/{vid}.{tr.lang or 'xx'}.{tr.raw_ext}", tr.raw)
            ado.set_state(
                wi_id, "subs",
                comment=(f"transcript ok: provider={tr.provider}, lang={tr.lang}, "
                         f"{len(tr.lines)} lines"),
                link=f"s3://{config.S3_BUCKET}/{key}" if key else "")
            print(f"  #{wi_id} {vid}: ok ({tr.provider}, {tr.lang})")
        except Exception as e:  # noqa: BLE001
            ado.set_state(wi_id, "failed", comment=f"subs error: {e}")
            print(f"  #{wi_id} {vid}: FAIL {e}")
        time.sleep(config.YTDLP_SLEEP_SECONDS)  # пауза против 429 (для ytdlp-ветки)


if __name__ == "__main__":
    main()
