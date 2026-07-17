"""Единый CI-тик кольца AutoMech: выбирает ОДНУ задачу конвейера (взвешенный
рандом со стирингом по очереди), делает её в таймбоксе и передаёт эстафету
следующему аккаунту. Заменяет отдельные flow-воркфлоу и ADO-планировщик — вся
оркестрация теперь внутри кольца (pipeline/ci_trigger.ring_handoff).

Задачи:
  subs     — транскрипты: state:new -> тело тикета -> state:subs
  embed    — индексация:  state:distilled -> Qdrant -> state:indexed
  delta    — дельта видео активных каналов -> новые Task'и state:new
  discover — поиск НОВЫХ каналов (1-2 seed-запроса, ~минута) -> эпики
  forums   — краул форума зоны в CI (низкий вес; основной парсинг форумов локальный)
Плюс backup — НЕ рандом, а гард «раз/сутки»: если сегодня бэкапа не было, тик
делает бэкап (эпики + indexed -> GitHub) и на этом завершает тик.

Дистилляцию (subs -> RepairCase) делает облачный Claude-агент, НЕ CI.

Стиринг: если в state:new пусто — subs не выбираем; если distilled пусто — embed
не выбираем (не тратим тик впустую). worked=True сбрасывает idle кольца, worked=
False растит его; кольцо встаёт, когда полный круг прошёл без работы.

    python scripts/ci_tick.py [--batch 10] [--partition solo] [--task subs]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.ado import AdoClient            # noqa: E402
from pipeline.ci_budget import guard          # noqa: E402
from pipeline.ci_trigger import ring_handoff  # noqa: E402


def _has(ado, state: str) -> bool:
    return bool(ado.query_by_state(state, top=1))


def _choose_task(ado) -> str:
    """Взвешенный рандом со стирингом: пустые стадии обнуляются."""
    weights = {
        "subs":     int(os.getenv("W_SUBS", "40")),
        "embed":    int(os.getenv("W_EMBED", "25")),
        "delta":    int(os.getenv("W_DELTA", "15")),
        "discover": int(os.getenv("W_DISCOVER", "8")),
        "forums":   int(os.getenv("W_FORUMS", "12")),
    }
    if not _has(ado, "new"):
        weights["subs"] = 0            # нечего транскрибировать
    if not _has(ado, "distilled"):
        weights["embed"] = 0           # нечего индексировать
    pool = [(t, w) for t, w in weights.items() if w > 0]
    if not pool:                        # подстраховка (не должно случаться)
        pool = [("delta", 1), ("discover", 1), ("forums", 1)]
    total = sum(w for _, w in pool)
    r = random.uniform(0, total)
    acc = 0.0
    for t, w in pool:
        acc += w
        if r <= acc:
            return t
    return pool[-1][0]


def _run_task(task: str, ado, batch: int, partition: str | None) -> bool:
    """Выполнить задачу. Возвращает worked (была ли реальная работа) для idle кольца."""
    if task == "subs":
        from ci_fetch_subs import fetch_subs_batch
        return fetch_subs_batch(ado, batch, partition) > 0

    if task == "embed":
        from embed_index_batch import embed_batch
        return embed_batch(ado, max(batch, 40), partition) > 0

    if task == "delta":
        from pipeline.youtube_discovery import (ensure_my_channels, load_channels,
                                                save_channels, sync_active_channels)
        ch = load_channels()
        n = (ensure_my_channels(ado, ch, True, 30)
             + sync_active_channels(ado, ch, True, 30))
        save_channels(ch)
        print(f"[tick] delta: +{n} видео в очередь")
        return n > 0

    if task == "discover":
        from pipeline.youtube_discovery import (SEED_QUERIES, discover_new_channels,
                                                load_channels, save_channels)
        ch = load_channels()
        qn = int(os.getenv("DISCOVER_QUERIES", "2"))
        qs = random.sample(SEED_QUERIES, min(qn, len(SEED_QUERIES)))
        n = discover_new_channels(ado, ch, True, queries=qs)
        save_channels(ch)
        print(f"[tick] discover: +{n} каналов ({len(qs)} запрос(ов))")
        return n > 0

    if task == "forums":
        from pipeline.crawler import crawl
        zone = os.getenv("CI_FORUM_ZONE", "b")     # b=EN (bimmerforums) — не пересекается
        t0 = time.monotonic()                       # с локальными drive2/vwvortex/autohome
        crawl(zone, float(os.getenv("CI_FORUM_MIN", "8")),
              create_workitems=True, max_threads=None, har=None)
        print(f"[tick] forums zone={zone}: {(time.monotonic() - t0) / 60:.1f} мин")
        return True

    print(f"[tick] неизвестная задача: {task}")
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=int(os.getenv("TICK_BATCH", "10")))
    ap.add_argument("--partition", default=os.getenv("PARTITION") or None)
    ap.add_argument("--task", default=None, help="принудительная задача (иначе рандом)")
    args = ap.parse_args()
    if args.partition in ("solo", ""):
        args.partition = None

    if not guard(20):                   # месячный лимит минут исчерпан -> пропуск,
        ring_handoff("tick", worked=False)   # но эстафету передаём дальше
        return

    ado = AdoClient()

    # 1) бэкап раз/сутки (не рандом): если сегодня не было — делаем и завершаем тик
    try:
        from pipeline.backup import run_backup
        if run_backup(ado):
            ring_handoff("tick", worked=True)
            return
    except Exception as e:              # noqa: BLE001 — бэкап не должен рвать тик
        print(f"[tick] backup error: {str(e)[:160]}")

    # 2) рандомная задача конвейера
    task = args.task or _choose_task(ado)
    print(f"[tick] задача: {task} (batch={args.batch}, partition={args.partition})")
    try:
        worked = _run_task(task, ado, args.batch, args.partition)
    except Exception as e:              # noqa: BLE001 — упавшая задача = пустой тик, кольцо едет
        print(f"[tick] задача {task} упала: {str(e)[:200]}")
        worked = False

    ring_handoff("tick", worked=worked)


if __name__ == "__main__":
    main()
