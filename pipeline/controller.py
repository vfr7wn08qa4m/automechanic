"""Контроллер конвейера (крутится в Azure DevOps по расписанию, раз в час).

Две функции = два ADO-пайплайна:

  delta    — поиск дельты: по активным каналам находит НОВЫЕ видео и заводит
             Task'и в состоянии New (дедуп по Custom.url / детям чанков).
             ado-pipelines/delta-hourly.yml

  dispatch — диспетчер: смотрит очередь по состояниям и триггерит CircleCI,
             РАСПРЕДЕЛЯЯ нагрузку между аккаунтами (по остатку бюджета минут,
             round-robin). ado-pipelines/dispatch-hourly.yml

CircleCI и claim в ADO делают остальное: любой аккаунт берёт первый свободный
work item, второй его уже не возьмёт (claim по System.Rev). Аккаунты можно
добавлять свободно — диспетчер сам распределит, куда есть бюджет.

    python -m pipeline.controller delta   [--max-videos 50]
    python -m pipeline.controller dispatch [--max-runs 6] [--dry-run]
    python -m pipeline.controller status
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import ci_budget, config
from .ado import STATE_MAP, AdoClient

BATCH = int(os.getenv("DISPATCH_BATCH", "10"))          # видео на один CI-прогон
EST_RUN_MINUTES = float(os.getenv("DISPATCH_EST_RUN_MINUTES", "20"))
# сколько работы «висит» на каждой стадии -> нужен прогон конвейера
WORK_STATES = ("new", "subs", "distilled")

# как часто delta делает тяжёлые под-шаги (troттлинг внутри частых прогонов)
DISCOVER_INTERVAL_H = float(os.getenv("DISCOVER_INTERVAL_H", "24"))   # поиск новых каналов
_STATE_KEY = "controller/state.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_ctrl_state() -> dict:
    if config.S3_ENDPOINT:
        try:
            from .store import s3_client
            body = s3_client().get_object(
                Bucket=config.S3_BUCKET, Key=_STATE_KEY)["Body"].read()
            return json.loads(body)
        except Exception:  # noqa: BLE001
            pass
    f = config.DATA_DIR / "controller_state.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _save_ctrl_state(st: dict) -> None:
    (config.DATA_DIR / "controller_state.json").write_text(
        json.dumps(st), encoding="utf-8")
    if config.S3_ENDPOINT:
        from .store import archive_blob
        archive_blob(_STATE_KEY, json.dumps(st))


def _hours_since(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return 1e9


# --- аккаунты CircleCI ---------------------------------------------------------

def load_ci_accounts() -> list[dict]:
    """Аккаунты CircleCI: из env CI_ACCOUNTS_JSON (в ADO — из variable group)
    или из локального accounts.json (пульт). Только с непустым токеном."""
    raw = os.getenv("CI_ACCOUNTS_JSON")
    if raw:
        accounts = json.loads(raw)
    else:
        panel = Path(__file__).resolve().parent.parent / "accounts.json"
        accounts = (json.loads(panel.read_text(encoding="utf-8")).get("accounts", [])
                    if panel.exists() else [])
    out = []
    for a in accounts:
        if a.get("circleci_token") and (a.get("circleci_project_slug")
                                        or a.get("circleci_definition_id")):
            out.append(a)
    return out


def trigger_circleci(account: dict, params: dict, dry_run: bool = False) -> str:
    """Дёрнуть пайплайн CircleCI. Новый API (pipeline/run + definition_id),
    иначе legacy (pipeline). Возвращает статус-строку."""
    token = account["circleci_token"]
    slug = account.get("circleci_project_slug", "")
    name = account.get("name", slug)
    if dry_run:
        return f"[dry] {name} <- {params}"
    headers = {"Circle-Token": token, "Content-Type": "application/json"}
    if account.get("circleci_definition_id"):
        url = f"https://circleci.com/api/v2/project/{slug}/pipeline/run"
        body = {"definition_id": account["circleci_definition_id"],
                "config": {"branch": "main"}, "checkout": {"branch": "main"},
                "parameters": params}
    else:
        url = f"https://circleci.com/api/v2/project/{slug}/pipeline"
        body = {"branch": "main", "parameters": params}
    r = requests.post(url, headers=headers, json=body, timeout=30)
    ok = r.status_code in (200, 201, 202)
    return f"{name}: HTTP {r.status_code} {'OK' if ok else r.text[:120]}"


# --- команды -------------------------------------------------------------------

def cmd_delta(max_videos: int, no_discover: bool = False) -> None:
    """Поиск дельты: свои каналы + активные каналы + (раз/сутки) поиск новых.
    Всё создаёт Task'и в New. Тяжёлый поиск каналов троттлится DISCOVER_INTERVAL_H."""
    from .youtube_discovery import (discover_new_channels, ensure_my_channels,
                                    load_channels, save_channels,
                                    sync_active_channels)
    ado = AdoClient()
    channels = load_channels()
    st = _load_ctrl_state()

    mine = ensure_my_channels(ado, channels, True, max_videos)
    print(f"delta: свои каналы -> +{mine} видео")

    new_epics = 0
    if not no_discover and _hours_since(st.get("last_discover")) >= DISCOVER_INTERVAL_H:
        new_epics = discover_new_channels(ado, channels, True)
        st["last_discover"] = _now().isoformat()
        print(f"delta: поиск новых каналов -> +{new_epics} эпиков")
    else:
        print("delta: поиск новых каналов пропущен (был недавно)")

    synced = sync_active_channels(ado, channels, True, max_videos)
    st["last_delta"] = _now().isoformat()
    save_channels(channels)
    _save_ctrl_state(st)
    print(f"delta ИТОГО: активные каналы +{synced} видео, новых эпиков {new_epics}")


def queue_counts(ado: AdoClient) -> dict[str, int]:
    return {s: len(ado.query_by_state(s, top=2000)) for s in WORK_STATES}


def cmd_dispatch(max_runs: int, dry_run: bool) -> None:
    ado = AdoClient()
    counts = queue_counts(ado)
    total_work = sum(counts.values())
    print(f"очередь: New={counts['new']} ReadyForFilter={counts['subs']} "
          f"ReadyForEmbeding={counts['distilled']} (всего {total_work})")
    if total_work == 0:
        print("работы нет — CircleCI не триггерим")
        return

    accounts = load_ci_accounts()
    if not accounts:
        print("нет аккаунтов CircleCI с токеном (заполни accounts.json / "
              "CI_ACCOUNTS_JSON)")
        return

    # доступные = у кого остался месячный бюджет минут
    avail = [a for a in accounts
             if ci_budget.remaining(a["name"]) >= EST_RUN_MINUTES]
    if not avail:
        print("все аккаунты исчерпали месячный бюджет минут — ждём след. месяца")
        return

    needed = min(math.ceil(total_work / BATCH), max_runs)
    # + по одному краулу на каждую зону форумов (форумы идут своим темпом,
    # но триггерим их из того же диспетчера)
    from .forum_sites import SITES
    zones = sorted({s.zone for s in SITES.values()})
    print(f"нужно conveyor-прогонов: {needed}; зоны краула: {zones}; "
          f"аккаунтов доступно: {len(avail)} ({', '.join(a['name'] for a in avail)})")

    jobs = [{"flow": "conveyor"} for _ in range(needed)]
    jobs += [{"flow": "crawl", "crawl-zone": z} for z in zones]

    # round-robin по аккаунтам с бюджетом
    triggered, i = 0, 0
    for job in jobs:
        for _hop in range(len(avail)):
            a = avail[i % len(avail)]
            i += 1
            if ci_budget.remaining(a["name"]) >= EST_RUN_MINUTES:
                print("  " + trigger_circleci(a, job, dry_run))
                triggered += 1
                break
        else:
            print("  все аккаунты исчерпали бюджет — стоп")
            break
    print(f"dispatch: запущено прогонов {triggered}")


def cmd_status() -> None:
    ado = AdoClient()
    counts = queue_counts(ado)
    print("очередь по состояниям:")
    for s in WORK_STATES:
        print(f"  {s:10} ({STATE_MAP[s]}): {counts[s]}")
    print(f"бюджет аккаунтов (использовано/лимит мин, стейт в {ci_budget.STORE}):")
    accs = load_ci_accounts() or [{"name": ci_budget.ACCOUNT}]
    for a in accs:
        u = ci_budget.used(a["name"])
        print(f"  {a['name']}: {u:.0f}/{ci_budget.CAP}  (остаток {ci_budget.CAP - u:.0f})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("delta")
    p1.add_argument("--max-videos", type=int, default=50)
    p1.add_argument("--no-discover", action="store_true",
                    help="не искать новые каналы в этом прогоне")
    p2 = sub.add_parser("dispatch")
    p2.add_argument("--max-runs", type=int, default=6)
    p2.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "delta":
        cmd_delta(args.max_videos, args.no_discover)
    elif args.cmd == "dispatch":
        cmd_dispatch(args.max_runs, args.dry_run)
    elif args.cmd == "status":
        cmd_status()


if __name__ == "__main__":
    main()
