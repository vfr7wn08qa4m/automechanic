"""Кольцо конвейера: CI-джоб в конце передаёт эстафету СЛЕДУЮЩЕМУ аккаунту в
кольце (acc1->acc2->...->accN->acc1), а не себе. Очередь ADO разгребается без
внешнего планировщика; в каждый момент молотит один аккаунт, бесплатный тир
CircleCI множится по кругу, поведение «размазано» по разным аккаунтам/IP.

КОЛЬЦО (приоритет) — по env, которые deploy.py кладёт в контекст 'automech'
per-account, указывая на СЛЕДУЮЩИЙ подключённый аккаунт:
    NEXT_CIRCLECI_TOKEN       — CircleCI PAT следующего аккаунта
    NEXT_CIRCLECI_PROJECT     — circleci/{org}/{proj} следующего
    NEXT_CIRCLECI_DEFINITION  — id pipeline-definition следующего
    RING_SIZE                 — число подключённых аккаунтов (порог стопа)
    RING_IDLE                 — сколько аккаунтов ПОДРЯД прошли вхолостую (эстафетный
                                параметр ring-idle: растёт при пустом тике, сбрасывается
                                в 0 при работе). Кольцо встаёт при RING_IDLE >= RING_SIZE,
                                т.е. когда полный круг прошёл без работы (очередь пуста).

ФОЛБЭК self-chain — если NEXT_* нет, дёргаем СВОЙ проект по env:
    CIRCLE_SELF_TOKEN / CIRCLE_PROJECT_SLUG / CIRCLE_DEFINITION_ID
Нет ни ring, ни self env (локальный прогон) -> эстафета выключена (no-op).
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


def can_ring() -> bool:
    return bool(_env("NEXT_CIRCLECI_TOKEN") and _env("NEXT_CIRCLECI_PROJECT")
                and _env("NEXT_CIRCLECI_DEFINITION"))


def can_selfchain() -> bool:
    return bool(_env("CIRCLE_SELF_TOKEN") and _env("CIRCLE_PROJECT_SLUG")
                and _env("CIRCLE_DEFINITION_ID"))


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
    """Запустить пайплайн СЛЕДУЮЩЕГО аккаунта в кольце (или свой — self-chain-фолбэк).
    Возвращает id пайплайна или None (эстафета выключена / ошибка)."""
    if can_ring():
        tok, slug, defid = (_env("NEXT_CIRCLECI_TOKEN"), _env("NEXT_CIRCLECI_PROJECT"),
                            _env("NEXT_CIRCLECI_DEFINITION"))
        who = "ring"
    else:
        tok, slug, defid = (_env("CIRCLE_SELF_TOKEN"), _env("CIRCLE_PROJECT_SLUG"),
                            _env("CIRCLE_DEFINITION_ID"))
        who = "self-chain"
    if not (tok and slug and defid):
        return None
    params: dict = {"flow": flow}
    if partition:
        params["partition"] = partition
    if zone:
        params["crawl-zone"] = zone
    if batch:
        params["batch-size"] = int(batch)
    if idle is not None:
        params["ring-idle"] = int(idle)
    return _run_pipeline(tok, slug, defid, params, who, flow)


def ring_handoff(flow: str, *, worked: bool, partition: str | None = None,
                 zone: str | None = None, batch: int | None = None) -> str | None:
    """Единая точка передачи эстафеты следующему аккаунту (кольцо) / себе (self-chain).

    worked=True  — этот аккаунт реально поработал: idle сбрасывается в 0.
    worked=False — пропустил тик (бюджет исчерпан / нет работы прямо сейчас): idle+1.
                   Эстафета всё равно уходит дальше — упёршийся аккаунт кольцо не рвёт.
    Кольцо останавливается, когда полный круг прошёл вхолостую (idle >= RING_SIZE) —
    значит очередь ADO разгребена. Защита от вечного холостого кручения.

    Фолбэк (нет NEXT_*): прежнее self-chain-поведение — свой следующий прогон
    дёргаем только если поработали (worked), иначе цепочка тихо встаёт."""
    if can_ring():
        size = _int_env("RING_SIZE", 1)
        next_idle = 0 if worked else _int_env("RING_IDLE", 0) + 1
        if size and next_idle >= size:
            print(f"[ring] полный пустой круг ({next_idle}/{size}) — "
                  f"эстафета остановлена (очередь разгребена)")
            return None
        return trigger(flow, partition=partition, zone=zone, batch=batch,
                       idle=next_idle)
    if can_selfchain() and worked:
        return trigger(flow, partition=partition, zone=zone, batch=batch)
    return None
