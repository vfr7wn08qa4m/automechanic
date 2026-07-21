"""Единый CI-тик кольца AutoMech: выбирает ОДНУ задачу конвейера (взвешенный
рандом со стирингом по очереди), делает её в таймбоксе и передаёт эстафету
следующему аккаунту. Заменяет отдельные flow-воркфлоу и ADO-планировщик — вся
оркестрация теперь внутри кольца (pipeline/ci_trigger.ring_handoff).

Задачи (веса в пуле):
  subs (35%)   — youtube_transcripts: state:new -> транскрипт+комменты -> state:subs
  forums (30%) — forum_posts: краул форума -> новые посты -> state:subs
  delta (20%)  — sync_youtube_channel_videos: активные каналы -> новые видео -> state:new
  embed (10%)  — index_to_qdrant: state:distilled -> embedding -> Qdrant -> state:indexed
  discover (5%) — discover_youtube_channels: seed-запросы (~1мин) -> новые каналы
Backup — НЕ рандом, а гард «раз/сутки»: если сегодня бэкапа не было, тик
делает бэкап (эпики + indexed -> GitHub) и завершает тик (эстафета передаётся).

Дистилляцию (state:subs -> RepairCase) делает облачный Claude-агент, НЕ CI.
Кольцо: ring_handoff() передаёт эстафету следующему аккаунту GitHub.

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
        "subs":     int(os.getenv("W_SUBS", "35")),      # youtube_transcripts
        "forums":   int(os.getenv("W_FORUMS", "30")),    # forum_posts
        "delta":    int(os.getenv("W_DELTA", "20")),     # sync_youtube_channel_videos
        "embed":    int(os.getenv("W_EMBED", "10")),     # index_to_qdrant
        "discover": int(os.getenv("W_DISCOVER", "5")),   # discover_youtube_channels
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
        from scripts.ci_fetch_subs import fetch_subs_batch
        worked = fetch_subs_batch(ado, batch, partition) > 0
        if not worked:
            # subs упал (tubetranscript или другое) -> выбираем ДРУГУЮ задачу прямо сейчас
            print(f"[tick] subs: 0 результатов, выбираю другую задачу из пула")
            alt_tasks = ["forums", "delta", "discover", "embed"]
            alt_task = random.choice(alt_tasks)
            print(f"[tick] fallback: вместо subs выполняю {alt_task}")
            return _run_task(alt_task, ado, batch, partition)
        return worked

    if task == "embed":
        from scripts.embed_index_batch import embed_batch
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
    ap.add_argument("--batch", type=int, default=int(os.getenv("TICK_BATCH", "3")))
    ap.add_argument("--partition", default=os.getenv("PARTITION") or None)
    ap.add_argument("--task", default=None, help="принудительная задача (иначе рандом)")
    ap.add_argument("--run-all", action="store_true", help="выполнить ВСЕ задачи по цепочке")
    args = ap.parse_args()
    if args.partition in ("solo", ""):
        args.partition = None

    print(f"[tick] ========== START ==========")
    print(f"[tick] batch={args.batch}, partition={args.partition}, task={args.task}, run_all={args.run_all}")
    print(f"[tick] python={sys.version.split()[0]}, env={os.getenv('GITHUB_ACTOR', 'unknown')}")

    print(f"[tick] проверка бюджета...")
    if not guard(20):                   # месячный лимит минут исчерпан -> пропуск,
        print(f"[tick] бюджет исчерпан, пропуск")
        ring_handoff("tick", worked=False)   # но эстафету передаём дальше
        return

    print(f"[tick] подключение к ADO...")
    ado = AdoClient()
    print(f"[tick] ADO подключен: org={ado.org}, project={ado.project}")

    # 1) бэкап раз/сутки (не рандом): если сегодня не было — делаем и завершаем тик
    print(f"[tick] проверка дневного бэкапа...")
    try:
        from pipeline.backup import run_backup
        backup_result = run_backup(ado)
        if backup_result:
            print(f"[tick] бэкап выполнен успешно, тик завершён")
            ring_handoff("tick", worked=True)
            return
        else:
            print(f"[tick] бэкап не требуется (уже был сегодня)")
    except Exception as e:              # noqa: BLE001 — бэкап не должен рвать тик
        print(f"[tick] backup error: {str(e)[:160]}")

    # диагностика состояния очереди
    try:
        new_count = len(ado.query_by_state("new", top=100) or [])
        distilled_count = len(ado.query_by_state("distilled", top=100) or [])
        indexed_count = len(ado.query_by_state("indexed", top=100) or [])
        print(f"[tick] очередь: new={new_count}, distilled={distilled_count}, indexed={indexed_count}")
    except Exception as e:
        print(f"[tick] ошибка при запросе очереди: {str(e)[:100]}")

    # 2) выполнение задач
    if args.run_all:
        # Выполнить ВСЕ задачи по цепочке
        tasks = ["subs", "embed", "delta", "discover", "forums"]
        random.shuffle(tasks)  # перемешиваем порядок
        print(f"[tick] режим run_all: выполняю {len(tasks)} задач по цепочке")
        print(f"[tick] порядок: {tasks}")
        total_worked = False
        for j, task in enumerate(tasks, 1):
            try:
                worked = _run_task(task, ado, args.batch, args.partition)
                if worked:
                    print(f"[tick]   [{j}/{len(tasks)}] {task}: ✓ worked=True")
                else:
                    print(f"[tick]   [{j}/{len(tasks)}] {task}: ✗ worked=False")
                total_worked = total_worked or worked
            except Exception as e:
                print(f"[tick]   [{j}/{len(tasks)}] {task}: ✗ упала - {str(e)[:80]}")
        ring_handoff("tick", worked=total_worked)
        print(f"[tick] ========== END (run_all, total_worked={total_worked}) ==========")
    else:
        # Рандомная или принудительная задача (оригинальное поведение)
        task = args.task or _choose_task(ado)
        print(f"[tick] выбранная задача: {task} (batch={args.batch}, partition={args.partition})")
        try:
            worked = _run_task(task, ado, args.batch, args.partition)
            if worked:
                print(f"[tick] ✓ задача {task} выполнена успешно: worked=True")
            else:
                print(f"[tick] ✗ задача {task} не нашла работу: worked=False")
        except Exception as e:              # noqa: BLE001 — упавшая задача = пустой тик, кольцо едет
            print(f"[tick] ✗ задача {task} упала с ошибкой: {str(e)[:200]}")
            worked = False

        # Диагностика перед handoff
        if not worked:
            try:
                new_count = len(ado.query_by_state("new", top=100) or [])
                distilled_count = len(ado.query_by_state("distilled", top=100) or [])
                print(f"[tick] диагностика: new={new_count}, distilled={distilled_count} (после задачи)")
            except Exception as e:
                print(f"[tick] ошибка диагностики: {str(e)[:100]}")

        ring_handoff("tick", worked=worked)
        print(f"[tick] ========== END (worked={worked}, ring_idle будет сброшена={worked}) ==========")


if __name__ == "__main__":
    main()
