"""Кольцо конвейера: CI-джоб в конце передаёт эстафету СЛЕДУЮЩЕМУ аккаунту в
кольце (acc1→acc2→...→accN→acc1), а не себе. Очередь ADO разгребается без
внешнего планировщика; в каждый момент молотит один аккаунт, бесплатный GitHub
Actions множится по кругу, поведение «размазано» по разным аккаунтам/IP.

КОЛЬЦО (приоритет) — переменные GitHub Actions секретов, указывающие на
СЛЕДУЮЩИЙ подключённый аккаунт:
    NEXT_GITHUB_TOKEN         — PAT следующего GitHub аккаунта
    NEXT_GITHUB_REPO          — owner/repo следующего (например vfr7wn08qa4m/automechanic)
    NEXT_GITHUB_WORKFLOW      — имя workflow (например automech-tick)
    RING_SIZE                 — число подключённых аккаунтов (порог стопа)
    RING_IDLE                 — сколько аккаунтов ПОДРЯД прошли вхолостую (растёт при
                                пустом тике, сбрасывается в 0 при работе). Кольцо встаёт
                                при RING_IDLE >= RING_SIZE, т.е. полный круг без работы.

ФОЛБЭК (если NEXT_* нет): цепочка к себе не запускается (локальный прогон).
"""
from __future__ import annotations

import json
import os
import urllib.request


def _env(name: str) -> str | None:
    v = os.getenv(name)
    return v if v and v.strip() else None


def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(_env(name) or default)
    except (TypeError, ValueError):
        return default


def can_ring_github() -> bool:
    """Проверка кольца GitHub Actions."""
    return bool(_env("NEXT_GITHUB_TOKEN") and _env("NEXT_GITHUB_REPO")
                and _env("NEXT_GITHUB_WORKFLOW"))


def can_ring_circleci() -> bool:
    """Проверка кольца CircleCI (legacy)."""
    return bool(_env("NEXT_CIRCLECI_TOKEN") and _env("NEXT_CIRCLECI_PROJECT")
                and _env("NEXT_CIRCLECI_DEFINITION"))


def can_ring() -> bool:
    """Кольцо: GitHub Actions (приоритет) или CircleCI (legacy)."""
    return can_ring_github() or can_ring_circleci()


def _run_github_workflow(token: str, repo: str, workflow: str, inputs: dict,
                         who: str) -> str | None:
    """Запустить GitHub Actions workflow через workflow_dispatch API."""
    body = {"ref": "main", "inputs": inputs}
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"token {token}", "Content-Type": "application/json",
                 "Accept": "application/vnd.github.v3+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status in (201, 202, 204):
                tail = " ".join(f"{k}={v}" for k, v in inputs.items() if v)
                print(f"[{who}] -> GitHub {repo} {workflow} {tail}")
                return "dispatched"
            else:
                print(f"[{who}] GitHub API error: {r.status}")
                return None
    except Exception as e:  # noqa: BLE001
        print(f"[{who}] не удалось дёрнуть workflow: {str(e)[:160]}")
        return None


def _run_pipeline(tok: str, slug: str, defid: str, params: dict, who: str,
                  flow: str) -> str | None:
    body = {"definition_id": defid, "config": {"branch": "main"},
            "checkout": {"branch": "main"}, "parameters": params}
    req = urllib.request.Request(
        f"https://circleci.com/api/v2/project/{slug}/pipeline/run",
        data=json.dumps(body).encode(), method="POST",
        headers={"Circle-Token": tok, "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=30))
        tail = " ".join(f"{k}={v}" for k, v in params.items() if k != "flow")
        print(f"[{who}] -> flow={flow} {tail} => pipeline #{r.get('number')}")
        return r.get("id")
    except Exception as e:  # noqa: BLE001
        print(f"[{who}] не удалось дёрнуть следующий прогон: {str(e)[:160]}")
        return None


def trigger(flow: str, *, partition: str | None = None, zone: str | None = None,
            batch: int | None = None, idle: int | None = None) -> str | None:
    """Запустить пайплайн СЛЕДУЮЩЕГО аккаунта в кольце.
    Поддержка: GitHub Actions (приоритет) или CircleCI (legacy).
    Возвращает id пайплайна или None (эстафета выключена / ошибка)."""

    # 1) GitHub Actions кольцо
    if can_ring_github():
        tok, repo, workflow = (_env("NEXT_GITHUB_TOKEN"), _env("NEXT_GITHUB_REPO"),
                               _env("NEXT_GITHUB_WORKFLOW"))
        inputs: dict = {}
        if partition:
            inputs["partition"] = partition
        if zone:
            inputs["zone"] = zone
        if batch:
            inputs["batch"] = str(batch)
        if idle is not None:
            inputs["idle"] = str(idle)
        return _run_github_workflow(tok, repo, workflow, inputs, "ring")

    # 2) CircleCI кольцо (legacy)
    if can_ring_circleci():
        tok, slug, defid = (_env("NEXT_CIRCLECI_TOKEN"), _env("NEXT_CIRCLECI_PROJECT"),
                            _env("NEXT_CIRCLECI_DEFINITION"))
        params: dict = {"flow": flow}
        if partition:
            params["partition"] = partition
        if zone:
            params["crawl-zone"] = zone
        if batch:
            params["batch-size"] = int(batch)
        if idle is not None:
            params["ring-idle"] = int(idle)
        return _run_pipeline(tok, slug, defid, params, "ring", flow)

    return None


def ring_handoff(flow: str, *, worked: bool, partition: str | None = None,
                 zone: str | None = None, batch: int | None = None) -> str | None:
    """Единая точка передачи эстафеты следующему аккаунту в кольце.

    worked=True  — этот аккаунт реально поработал: idle сбрасывается в 0.
    worked=False — пропустил тик (бюджет исчерпан / нет работы): idle+1.
                   Эстафета всё равно уходит дальше — упёршийся аккаунт кольцо не рвёт.
    Кольцо останавливается, когда полный круг прошёл вхолостую (idle >= RING_SIZE) —
    значит очередь ADO разгребена. Защита от вечного холостого кручения.

    Поддержка: GitHub Actions (приоритет) или CircleCI (legacy).
    """
    print(f"[ring] ring_handoff called: worked={worked}")
    print(f"[ring] can_ring()={can_ring()}, can_ring_github()={can_ring_github()}, can_ring_circleci()={can_ring_circleci()}")
    print(f"[ring] NEXT_GITHUB_TOKEN={bool(_env('NEXT_GITHUB_TOKEN'))}, NEXT_GITHUB_REPO={bool(_env('NEXT_GITHUB_REPO'))}, NEXT_GITHUB_WORKFLOW={bool(_env('NEXT_GITHUB_WORKFLOW'))}")

    if can_ring():
        size = _int_env("RING_SIZE", 1)
        next_idle = 0 if worked else _int_env("RING_IDLE", 0) + 1
        print(f"[ring] size={size}, next_idle={next_idle} (RING_IDLE={_int_env('RING_IDLE', 0)})")
        if size and next_idle >= size:
            print(f"[ring] полный пустой круг ({next_idle}/{size}) — "
                  f"эстафета остановлена (очередь разгребена)")
            return None
        return trigger(flow, partition=partition, zone=zone, batch=batch,
                       idle=next_idle)
    print(f"[ring] can_ring() returned False - no handoff")
    return None
