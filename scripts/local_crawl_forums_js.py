"""Локальный краул форумов за JS-челленджем Cloudflare (зона D: vwvortex и др.) —
ДОМАШНИЙ IP + headless-браузер со stealth-твиками (pipeline/crawler.py) решают
челлендж, который CI-датацентр не проходит достаточно надёжно/предсказуемо.
Курсор/фронтир зоны переживает прогоны (см. pipeline/crawler.load_state/save_state).

    python scripts/local_crawl_forums_js.py --minutes 20            # налить порцию
    python scripts/local_crawl_forums_js.py --max-threads 10 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_az = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))["azure"]
os.environ.update(ADO_ORG=_az["org"], ADO_PROJECT=_az["project"], ADO_PAT=_az["pat"])

from pipeline.crawler import crawl   # noqa: E402

# Замок: не даём двум экземплярам крутиться разом (планировщик + ручной запуск).
LOCK = ROOT / "data" / "forumsjs.lock"
STALE_MIN = 45


def _acquire_lock() -> bool:
    LOCK.parent.mkdir(exist_ok=True)
    if LOCK.exists():
        age_min = (time.time() - LOCK.stat().st_mtime) / 60
        if age_min < STALE_MIN:
            print(f"forums-js: уже работает другой экземпляр (замок {age_min:.0f} мин) — выхожу")
            return False
        print(f"forums-js: замок протух ({age_min:.0f} мин) — перехватываю")
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--max-threads", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.dry_run and not _acquire_lock():
        return
    try:
        crawl("d", a.minutes, create_workitems=not a.dry_run,
              max_threads=a.max_threads, har=None)
    finally:
        if not a.dry_run:
            LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
