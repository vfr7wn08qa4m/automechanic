"""CI-этап «краул форумов»: обход тредов зоны -> посты в R2 -> тикеты ADO.

Тайм-бокс укладывает прогон в бюджет CI-джоба (<20 мин), фронтир сохраняется —
следующий прогон продолжает с места остановки.

    python scripts/ci_crawl.py --zone a --minutes 18
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.ci_budget import guard, record   # noqa: E402
from pipeline.crawler import crawl              # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zone", default=os.getenv("CRAWL_ZONE", "a"))
    ap.add_argument("--minutes", type=float, default=18.0)
    ap.add_argument("--max-threads", type=int, default=None)
    args = ap.parse_args()
    if not guard(args.minutes + 2):   # +2 на setup; месячный лимит исчерпан -> выход 0
        return
    import time
    t0 = time.monotonic()
    try:
        crawl(args.zone, args.minutes, create_workitems=True,
              max_threads=args.max_threads, har=None)
    finally:
        record((time.monotonic() - t0) / 60 - (args.minutes + 2))  # факт − резерв


if __name__ == "__main__":
    main()
