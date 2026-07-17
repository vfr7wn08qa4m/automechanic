"""Локальный краул drive2 (ДОМАШНИЙ IP) — бортжурналы -> тикеты ADO (state:subs).

drive2 за DDoS-Guard: облако/Worker-реле = 403. Работает ТОЛЬКО с резидентного
(домашнего) IP, поэтому этап локальный — запускай на своей машине.
Курсор по под-картам сохраняется (data/drive2_cursor.json) — повторный прогон
продолжает с места. ADO-дедуп не плодит дубли.

    python scripts/local_crawl_drive2.py --minutes 20            # налить порцию
    python scripts/local_crawl_drive2.py --max 10 --dry-run      # проверить без записи
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

from pipeline.drive2 import crawl_drive2   # noqa: E402

# Замок: не даём двум экземплярам крутиться разом (планировщик + ручной запуск).
# Второй видит свежий замок и выходит. Замок старше STALE_MIN считаем брошенным
# (машина уснула/скрипт упал) и перехватываем.
LOCK = ROOT / "data" / "drive2.lock"
STALE_MIN = 45


def _acquire_lock() -> bool:
    LOCK.parent.mkdir(exist_ok=True)
    if LOCK.exists():
        age_min = (time.time() - LOCK.stat().st_mtime) / 60
        if age_min < STALE_MIN:
            print(f"drive2: уже работает другой экземпляр (замок {age_min:.0f} мин) — выхожу")
            return False
        print(f"drive2: замок протух ({age_min:.0f} мин) — перехватываю")
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--max", type=int, default=None, dest="max_entries")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.dry_run and not _acquire_lock():
        return
    try:
        crawl_drive2(a.minutes, a.max_entries, create_workitems=not a.dry_run)
    finally:
        if not a.dry_run:
            LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
