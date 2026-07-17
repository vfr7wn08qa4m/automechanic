"""CI-этап «краул autohome» (китайский Q&A через Worker-реле) -> тикеты ADO.

    python scripts/ci_crawl_autohome.py --minutes 16
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.autohome import crawl_autohome        # noqa: E402
from pipeline.ci_budget import guard, record         # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=16.0)
    ap.add_argument("--max-threads", type=int, default=None)
    args = ap.parse_args()
    if not guard(args.minutes + 2):     # лимит исчерпан -> пропуск тика, эстафета дальше
        from pipeline.ci_trigger import ring_handoff
        ring_handoff("crawl-cn", worked=False)
        return
    t0 = time.monotonic()
    try:
        crawl_autohome(args.minutes, args.max_threads, create_workitems=True)
    finally:
        elapsed = (time.monotonic() - t0) / 60
        record(elapsed - (args.minutes + 2))

    # эстафета autohome-краула следующему аккаунту: worked=таймбокс отработал.
    # Пустой тик -> idle+1, но эстафета уходит дальше (кольцо не рвём).
    from pipeline.ci_trigger import ring_handoff
    ring_handoff("crawl-cn", worked=elapsed >= args.minutes * 0.6)


if __name__ == "__main__":
    main()
