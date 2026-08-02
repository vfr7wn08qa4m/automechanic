"""Единый CI-тик кольца AutoMech: выбирает ОДНУ задачу конвейера (взвешенный
рандом со стирингом по очереди), делает её в таймбоксе и передаёт эстафету
следующему аккаунту. Заменяет отдельные flow-воркфлоу и ADO-планировщик — вся
оркестрация теперь внутри кольца (pipeline/ci_trigger.ring_handoff).

Задачи (веса в пуле):
  subs (35%)    — youtube_transcripts: state:new -> транскрипт+комменты -> state:subs
  forums (30%)  — forum_posts: краул форума -> новые посты -> state:subs
  delta (20%)   — sync_youtube_channel_videos: активные каналы -> новые видео -> state:new
  distill (15%) — LLM-дистилляция: state:subs -> RepairCase -> state:distilled/offtopic
  embed (10%)   — index_to_qdrant: state:distilled -> embedding -> Qdrant -> state:indexed
  asr (15%)     — Whisper для видео БЕЗ титров -> state:subs
  discover (5%) — discover_youtube_channels: seed-запросы (~1мин) -> новые каналы
Backup — НЕ рандом, а гард «раз/сутки»: если сегодня бэкапа не было, тик
делает бэкап (эпики + indexed -> GitHub) и завершает тик (эстафета передаётся).

Кольцо: ring_handoff() передаёт эстафету следующему аккаунту GitHub.

Стиринг: если в state:new пусто — subs не выбираем; если state:subs пусто —
 distill не выбираем; если distilled пусто — embed не выбираем (не тратим тик
впустую). worked=True сбрасывает idle кольца, worked=False растит его; кольцо
встаёт, когда полный круг прошёл без работы.

ДИНАМИЧЕСКИЕ ВЕСА (2026-07-31): проценты выше — лишь БАЗА, дальше они
домножаются на реальную глубину очередей (_FLOW/_dyn_factors, точный счёт через
ado.state_counts()). Тик мигрирует туда, где скопилось: стадия с забитым входом
получает до x3, а всё, что наливает в уже забитую очередь, глушится до x0.05.
Пример живой раскладки при subs=48k: distill 68%, subs 16%, forums 4.6%
(вместо статических 15/35/30). Отключается DYNAMIC_WEIGHTS=0.

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


def _has_asr_backlog(ado) -> bool:
    """Есть ли видео-брак без титров, который ещё не пробовали через ASR."""
    try:
        proj = ado.project.replace("'", "''")
        q = ("SELECT [System.Id] FROM WorkItems "
             f"WHERE [System.TeamProject] = '{proj}' AND [System.State] = 'Removed' "
             "AND [System.Tags] CONTAINS 'auto-mech' AND [System.Tags] CONTAINS 'failed' "
             "AND [System.Tags] NOT CONTAINS 'asr-failed' "
             "AND [System.Title] CONTAINS 'vid:' "
             "AND [System.Title] NOT CONTAINS 'vid:frm-' "
             "AND [System.Title] NOT CONTAINS 'vid:cck-'")
        return bool(ado._wiql(q, top=1))
    except Exception:  # noqa: BLE001 — стиринг не должен ронять тик
        return False


def _ensure_whispercpp() -> bool:
    """Собрать whisper.cpp и скачать ggml-модель ТОЛЬКО когда дошли до ASR-задачи.

    В общий pip/apt воркфлоу это не кладём: сборка ~1-2 мин, а задача asr выпадает
    редко — тормозить каждый тик незачем. Раннер между джобами не сохраняется,
    поэтому собираем в рабочей копии и переиспользуем в пределах джоба.

    Зачем вообще: у Cloudflare 10k нейронов/сутки на аккаунт и квота ОБЩАЯ с
    эмбеддингом, Whisper выжигает её первым (2026-07-30 ASR встал на 429 по всему
    кольцу). whisper.cpp — CPU-бинарник без квот, платим только минутами GitHub.
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    repo = root / "whisper.cpp"
    model_name = os.getenv("ASR_LOCAL_MODEL", "base")
    binary = repo / "build" / "bin" / "whisper-cli"
    model = repo / "models" / f"ggml-{model_name}.bin"
    if binary.exists() and model.exists():
        os.environ.setdefault("WHISPER_CPP_BIN", str(binary))
        os.environ.setdefault("WHISPER_CPP_MODEL", str(model))
        return True

    try:
        if not repo.exists():
            print("[tick] клонирую whisper.cpp...")
            subprocess.run(["git", "clone", "--depth", "1",
                            "https://github.com/ggml-org/whisper.cpp", str(repo)],
                           check=True, capture_output=True, text=True, timeout=300)
        if not binary.exists():
            print("[tick] собираю whisper.cpp (cmake, ~1-2 мин)...")
            subprocess.run(["cmake", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"],
                           cwd=repo, check=True, capture_output=True, text=True,
                           timeout=900)
            subprocess.run(["cmake", "--build", "build", "-j", "--config", "Release"],
                           cwd=repo, check=True, capture_output=True, text=True,
                           timeout=1800)
        if not model.exists():
            print(f"[tick] скачиваю ggml-модель {model_name}...")
            subprocess.run(["bash", "./models/download-ggml-model.sh", model_name],
                           cwd=repo, check=True, capture_output=True, text=True,
                           timeout=900)
    except subprocess.CalledProcessError as e:
        print(f"[tick] whisper.cpp не собрался: {(e.stderr or '')[-400:]}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[tick] whisper.cpp: {type(e).__name__} {str(e)[:200]}")
        return False

    if not (binary.exists() and model.exists()):
        print("[tick] whisper.cpp: бинарь/модель так и не появились")
        return False
    os.environ["WHISPER_CPP_BIN"] = str(binary)
    os.environ["WHISPER_CPP_MODEL"] = str(model)
    print(f"[tick] whisper.cpp готов: {binary.name} + ggml-{model_name}.bin")
    return True


# Поток конвейера: (какую очередь задача РАЗГРЕБАЕТ, какую НАПОЛНЯЕТ).
# None — стадии нет: у asr/forums вход не очередь ADO (свой бэклог/внешний сайт),
# у embed выход конечный (Qdrant), дальше копиться нечему.
# ВАЖНО: subs и asr — одновременно и потребители, и производители. Они разгребают
# new/бэклог, но кладут результат в ТУ ЖЕ очередь subs, которую разгребает distill.
# Без учёта выхода они выглядели «полезными» и получали 60% тиков, продолжая
# наливать в очередь, где уже 48k ждут дистилляции.
_FLOW = {
    "subs":     ("new", "subs"),
    "asr":      (None, "subs"),
    "forums":   (None, "subs"),
    "delta":    (None, "new"),
    "discover": (None, "new"),
    "distill":  ("subs", "distilled"),
    "embed":    ("distilled", None),
}
# «Здоровая» глубина очереди: на ней множители равны 1.0 и веса = базовым.
_QUEUE_TARGET = float(os.getenv("QUEUE_TARGET", "500"))


def _dyn_factors(counts: dict) -> dict[str, float]:
    """Множители к базовым весам по РЕАЛЬНОЙ глубине очередей.

    За вход: чем глубже, тем выше вес (до x3) — тик мигрирует туда, где скопилось.
    За выход: чем глубже, тем ниже вес (до x0.05) — незачем лить ещё, когда там
    уже десятки тысяч ждут. Корень (а не линейно), чтобы очередь на два порядка
    больше не выжирала пул целиком: перекос должен смещать приоритет, а не
    превращать кольцо в однозадачное (иначе упавшая по квоте стадия застопорит
    всё кольцо — idle растёт, работа не делается).
    """
    f: dict[str, float] = {}
    for task, (src, dst) in _FLOW.items():
        k = 1.0
        if src is not None:
            d = counts.get(src, 0)
            if d <= 0:
                f[task] = 0.0            # вход пуст — стадию не выбираем вовсе
                continue
            k *= min(3.0, max(0.5, (d / _QUEUE_TARGET) ** 0.5))
        if dst is not None:
            d = counts.get(dst, 0)
            if d > _QUEUE_TARGET:
                k *= max(0.05, (_QUEUE_TARGET / d) ** 0.5)
        f[task] = k
    return f


def _choose_task(ado) -> str:
    """Взвешенный рандом со стирингом: пустые стадии обнуляются."""
    weights: dict[str, float] = {
        "subs":     float(os.getenv("W_SUBS", "35")),    # youtube_transcripts
        "forums":   float(os.getenv("W_FORUMS", "30")),  # forum_posts
        "delta":    float(os.getenv("W_DELTA", "20")),   # sync_youtube_channel_videos
        "distill":  float(os.getenv("W_DISTILL", "15")), # LLM-дистилляция
        "embed":    float(os.getenv("W_EMBED", "10")),   # index_to_qdrant
        "asr":      float(os.getenv("W_ASR", "15")),     # Whisper для видео БЕЗ титров
        "discover": float(os.getenv("W_DISCOVER", "5")), # discover_youtube_channels
    }
    # ДИНАМИКА. Один запрос к Analytics отдаёт точные глубины всех очередей
    # (WIQL этого не может — режется лимитом 20000, а subs уже >47k). Analytics
    # недоступен -> counts пуст -> работаем на статических весах, как раньше.
    if os.getenv("DYNAMIC_WEIGHTS", "1") != "0":
        counts = ado.state_counts()
        if counts:
            fac = _dyn_factors(counts)
            for t, k in fac.items():
                if t in weights:
                    weights[t] *= k
            print(f"[tick] очереди: " + ", ".join(
                f"{q}={counts.get(q, 0)}" for q in ("new", "subs", "distilled")))
            print(f"[tick] динамические веса: " + ", ".join(
                f"{t}={w:.1f}" for t, w in sorted(weights.items(), key=lambda x: -x[1])))
    if not _has(ado, "new"):
        weights["subs"] = 0            # нечего транскрибировать
    if not _has(ado, "subs"):
        weights["distill"] = 0         # нечего дистиллировать
    if not _has(ado, "distilled"):
        weights["embed"] = 0           # нечего индексировать
    if weights.get("asr") and not _has_asr_backlog(ado):
        weights["asr"] = 0
    # ASR УСТУПАЕТ ЭМБЕДДИНГУ. Whisper и bge-m3 живут на ОДНОМ аккаунте Cloudflare,
    # у которого 10 000 нейронов в сутки на всё кольцо, и Whisper дороже на порядки.
    # Пока в state:distilled есть готовые кейсы, тратить квоту на распознавание нельзя:
    # 2026-07-29 из-за этого за сутки не векторизовалось НИ ОДНОГО кейса (CF 429).
    # ASR — обогащение, эмбеддинг — конечный продукт конвейера.
    # ...но это верно ТОЛЬКО пока ASR ходит в Cloudflare. С whisper.cpp на раннере
    # распознавание не трогает квоту CF вовсе, и морить ASR голодом больше незачем
    # (проверено в кольце 2026-07-30: whisper.cpp дал 33 строки без единого нейрона CF).
    _asr_uses_cf_only = "whispercpp" not in os.getenv(
        "ASR_PROVIDERS", "cloudflare,whispercpp")
    if weights.get("asr") and _asr_uses_cf_only and _has(ado, "distilled"):
        weights["asr"] = 0             # есть что векторизовать — квоту CF отдаём эмбеддингу
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

    if task == "distill":
        # Перед тем как трогать очередь state:subs, проверяем API дистилляции в облаке.
        from pipeline.distill import healthcheck as distill_healthcheck
        ok, msg = distill_healthcheck()
        print(f"[tick] distill healthcheck: {msg}")
        if not ok:
            print("[tick] distill API недоступен, очередь state:subs не трогаем")
            return False
        from scripts.local_distill_batch import distill_batch
        # СВОЙ размер порции, независимый от общего --batch. Дистилляция — узкое
        # место конвейера, и общий batch (по умолчанию 3) держал её на ~1090
        # кейсов в сутки, тогда как форумы наливают ~1365 — очередь росла, а не
        # таяла. Пул ключей Gemini позволяет больше: порция 5 даёт ~1800/сутки и
        # разворачивает баланс. Верхний предел всё равно нужен — дистилляция это
        # долгий API-вызов, и один тик не должен съесть весь таймаут джобы (45 мин).
        n = distill_batch(ado, int(os.getenv("DISTILL_BATCH", "5")), partition)
        return n > 0

    if task == "embed":
        from scripts.embed_index_batch import embed_batch
        return embed_batch(ado, max(batch, 40), partition) > 0

    if task == "asr":
        # Видео БЕЗ титров: аудио берут convert1s/rapidapi (сторонние YouTube→MP3 —
        # тянут YouTube на СВОИХ серверах, поэтому датацентр-блок обходится).
        # Распознавание: cloudflare (дёшево по времени раннера, но 10k нейронов в
        # сутки на аккаунт, и с эмбеддингом квота ОБЩАЯ) -> local faster-whisper
        # НА РАННЕРЕ (квоту CF не тратит вовсе, платим только минутами GitHub).
        # 2026-07-30: ASR встал именно на «cloudflare: 429» — локальный Whisper
        # снимает этот потолок, поэтому он вторым в цепочке.
        os.environ.setdefault("AUDIO_PROVIDERS", "convert1s,rapidapi")
        os.environ.setdefault("ASR_PROVIDERS", "cloudflare,whispercpp")
        os.environ.setdefault("ASR_LOCAL_MODEL", "base")
        if "whispercpp" in os.environ["ASR_PROVIDERS"]:
            _ensure_whispercpp()
        from scripts.asr_backfill import backfill
        return backfill(ado, min(batch, 5)) > 0

    if task == "delta":
        from pipeline.youtube_discovery import (ensure_my_channels, load_channels,
                                                save_channels, sync_active_channels)
        ch = load_channels()
        # Тайм-бокс обязателен: без него цикл идёт по ВСЕМ активным каналам без
        # остановки и на большом их числе упирается в жёсткий timeout-minutes (45)
        # джобы — GitHub убивает джобу по таймауту (статус "cancelled", найдено
        # 2026-07-31 живьём на gh15). CI_DELTA_MIN оставляет запас под setup-шаги.
        deadline = time.monotonic() + float(os.getenv("CI_DELTA_MIN", "12")) * 60
        n = (ensure_my_channels(ado, ch, True, 30, deadline=deadline)
             + sync_active_channels(ado, ch, True, 30, deadline=deadline))
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

    print(f"[tick] подключение к ADO...")
    ado = AdoClient()
    print(f"[tick] ADO подключен: org={ado.org}, project={ado.project}")

    # Общий замок эстафеты (см. pipeline/ado.py::ring_lock_acquire). Каждый ручной
    # workflow_dispatch раньше запускал СВОЮ независимую цепочку — ring_handoff в
    # конце тика просто дёргал следующий аккаунт, не проверяя, бежит ли уже где-то
    # ДРУГАЯ эстафета. concurrency: в workflow не спасает — она держит один прогон
    # только внутри ОДНОГО репозитория, а тут 22 разных. 2026-07-30: несколько
    # тестовых dispatch'ей за день породили несколько параллельных цепочек, кольцо
    # крутило 3-4 сканера разом. Если замок занят другим (свежим) прогоном — эта
    # лишняя цепочка тихо гаснет здесь: ЗАДАЧУ НЕ ДЕЛАЕМ И ring_handoff НЕ ЗОВЁМ,
    # иначе она бы сама породила ещё один хоп дальше вместо того чтобы схлопнуться.
    holder = f"{os.getenv('GITHUB_ACTOR', 'unknown')}#{os.getenv('GITHUB_RUN_ID', '0')}"
    if not ado.ring_lock_acquire(holder):
        print(f"[tick] замок эстафеты занят другим прогоном — эта цепочка "
              f"гаснет (ring_handoff НЕ вызывается)")
        return
    print(f"[tick] замок эстафеты захвачен ({holder})")

    try:
        _main_locked(ado, args, holder)
    finally:
        ado.ring_lock_release(holder)


def _main_locked(ado, args, holder: str) -> None:
    print(f"[tick] проверка бюджета...")
    if not guard(20):                   # месячный лимит минут исчерпан -> пропуск,
        print(f"[tick] бюджет исчерпан, пропуск")
        ring_handoff("tick", worked=False)   # но эстафету передаём дальше
        return

    # диагностика состояния очереди ДО РАБОТЫ
    try:
        new_count = len(ado.query_by_state("new", top=100) or [])
        distilled_count = len(ado.query_by_state("distilled", top=100) or [])
        indexed_count = len(ado.query_by_state("indexed", top=100) or [])
        queue_total = new_count + distilled_count
        print(f"[tick] очередь: new={new_count}, distilled={distilled_count}, indexed={indexed_count} (total={queue_total})")
    except Exception as e:
        print(f"[tick] ошибка при запросе очереди: {str(e)[:100]}")
        queue_total = 0

    # SKIP бэкап если в очереди > 1000 итемов (приоритет: обработка задач ≫ бэкап)
    should_skip_backup = queue_total > 1000
    if should_skip_backup:
        print(f"[tick] [!] очередь перегружена ({queue_total} итемов) — пропускаем бэкап, идём в работу")

    # выполнение задач (ОСНОВНАЯ РАБОТА — приоритет выше бэкапа)
    if args.run_all:
        # Выполнить ВСЕ задачи по цепочке
        tasks = ["subs", "distill", "embed", "delta", "discover", "forums"]
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
        # отметить в замке, чем заняты — борд показывает это вживую (см. ado.ring_lock_note)
        ado.ring_lock_note(holder, task)
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

        print(f"[tick] выполняю ring_handoff(worked={worked})...")
        result = ring_handoff("tick", worked=worked)
        print(f"[tick] ring_handoff вернул: {result}")

    # 3) бэкап в КОНЦЕ (после работы, и только если очередь не перегружена)
    if not should_skip_backup:
        try:
            from pipeline.backup import run_backup
            backup_result = run_backup(ado)
            if backup_result:
                print(f"[tick] [+] бэкап выполнен успешно в конце тика")
            else:
                print(f"[tick] бэкап не требуется (уже был сегодня)")
        except Exception as e:
            print(f"[tick] backup error: {str(e)[:160]}")
    else:
        print(f"[tick] [!] бэкап пропущен (очередь перегружена)")

    print(f"[tick] ========== END (worked={worked}, ring_idle будет сброшена={worked}) ==========")


if __name__ == "__main__":
    main()
