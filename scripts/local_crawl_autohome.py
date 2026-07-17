"""Локальный краул autohome (ДОМАШНИЙ IP) — китайский Q&A -> тикеты ADO (state:subs).

autohome режет датацентр/реле, прямой домашний IP пускает. pageSize=1000 -> весь пул
hot Q&A (~2972 темы). ADO-дедуп = резюме между прогонами (курсор не нужен, пул мал).

    python scripts/local_crawl_autohome.py --minutes 20            # налить порцию
    python scripts/local_crawl_autohome.py --max-threads 5 --dry-run  # проверить без записи
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

from pipeline.autohome import crawl_autohome   # noqa: E402

# Замок: не даём двум экземплярам крутиться разом (планировщик + ручной запуск).
LOCK = ROOT / "data" / "autohome.lock"
STALE_MIN = 45


def _acquire_lock() -> bool:
    LOCK.parent.mkdir(exist_ok=True)
    if LOCK.exists():
        age_min = (time.time() - LOCK.stat().st_mtime) / 60
        if age_min < STALE_MIN:
            print(f"autohome: уже работает другой экземпляр (замок {age_min:.0f} мин) — выхожу")
            return False
        print(f"autohome: замок протух ({age_min:.0f} мин) — перехватываю")
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
        crawl_autohome(a.minutes, a.max_threads, create_workitems=not a.dry_run)
    finally:
        if not a.dry_run:
            LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
