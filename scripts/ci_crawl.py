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
    if not guard(args.minutes + 2):   # +2 на setup; месячный лимит исчерпан -> пропуск
        from pipeline.ci_trigger import ring_handoff   # тика, но эстафету передаём дальше
        ring_handoff("crawl-js" if args.zone == "d" else "crawl",
                     worked=False, zone=args.zone)
        return
    import time
    t0 = time.monotonic()
    try:
        crawl(args.zone, args.minutes, create_workitems=True,
              max_threads=args.max_threads, har=None)
    finally:
        elapsed = (time.monotonic() - t0) / 60
        record(elapsed - (args.minutes + 2))  # факт − резерв

    # эстафета краула зоны следующему аккаунту в кольце: worked=таймбокс реально
    # отработал (фронтир не пуст). Пустой тик -> idle+1, эстафета всё равно уходит
    # дальше. Бюджет-throttle тут не нужен: у следующего аккаунта свой бюджет, а
    # guard() на старте каждого прогона сам тормозит исчерпанный аккаунт.
    from pipeline.ci_trigger import ring_handoff
    worked_full = elapsed >= args.minutes * 0.6
    ring_handoff("crawl-js" if args.zone == "d" else "crawl",
                 worked=worked_full, zone=args.zone)

    # авто-индексация отдельным лейном: Claude накопил distilled -> пинок index
    # следующему аккаунту (embed-index сам разгребёт бэклог -> Closed + поиск).
    from pipeline.ado import AdoClient
    if len(AdoClient().query_by_state("distilled", top=3)) >= 3:
        ring_handoff("index", worked=True, batch=20)


if __name__ == "__main__":
    main()
