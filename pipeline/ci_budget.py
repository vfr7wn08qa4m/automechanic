"""Бюджет-гард минут CircleCI: не запускать этап, если месячный лимит исчерпан.

Стейт хранится В AZURE DEVOPS (по умолчанию): помесячный work item-леджер на
аккаунт (тип Feature, минуты в поле Effort) — один пайплайн между прогонами
читает/пишет стейт там, без внешних зависимостей. Это и есть авто-распределение
нагрузки между X аккаунтами: диспетчер читает остаток каждого и грузит туда, где
свободно; сам аккаунт тормозит через guard(), когда исчерпал лимит. Фолбэк —
R2/локальный файл (CI_BUDGET_STORE=r2).

Каждый CI-джоб в начале зовёт guard(estimate): использовано+оценка > лимита ->
False (джоб выходит 0); иначе резервирует оценку и в конце уточняет record().

Env: CI_MONTHLY_MINUTES (лимит, 6000), CI_ACCOUNT (метка), CI_BUDGET_STORE (ado|r2).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import config

CAP = int(os.getenv("CI_MONTHLY_MINUTES", "6000"))
ACCOUNT = os.getenv("CI_ACCOUNT", "solo")
STORE = os.getenv("CI_BUDGET_STORE", "ado")


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _ado():
    """AdoClient, если выбран ADO-бэкенд и заданы креды; иначе None (R2-фолбэк)."""
    if STORE == "ado" and config.ADO_ORG and config.ADO_PROJECT and config.ADO_PAT:
        from .ado import AdoClient
        try:
            return AdoClient()
        except Exception:  # noqa: BLE001
            return None
    return None


# --- R2/локальный фолбэк -------------------------------------------------------

def _r2_key(account: str) -> str:
    return f"budget/{account}/{_month()}.json"


def _r2_used(account: str) -> float:
    if config.S3_ENDPOINT:
        try:
            from .store import s3_client
            body = s3_client().get_object(
                Bucket=config.S3_BUCKET, Key=_r2_key(account))["Body"].read()
            return json.loads(body).get("minutes", 0.0)
        except Exception:  # noqa: BLE001
            return 0.0
    local = config.DATA_DIR / f"budget_{account}.json"
    if local.exists():
        d = json.loads(local.read_text(encoding="utf-8"))
        if d.get("month") == _month():
            return d.get("minutes", 0.0)
    return 0.0


def _r2_write(minutes: float, account: str) -> None:
    payload = json.dumps({"minutes": round(minutes, 1), "month": _month(),
                          "account": account})
    (config.DATA_DIR / f"budget_{account}.json").write_text(payload, encoding="utf-8")
    if config.S3_ENDPOINT:
        from .store import archive_blob
        archive_blob(_r2_key(account), payload)


# --- публичный API -------------------------------------------------------------

def used(account: str | None = None) -> float:
    """Сколько минут израсходовано аккаунтом в этом месяце (read-only)."""
    account = account or ACCOUNT
    ado = _ado()
    if ado:
        wid = ado.budget_ledger(account, _month(), create=False)
        return ado.budget_read(wid) if wid else 0.0
    return _r2_used(account)


def remaining(account: str | None = None, cap: int | None = None) -> float:
    """Остаток минут аккаунта (для диспетчера — куда ещё можно грузить)."""
    return max(0.0, (cap or CAP) - used(account))


def guard(estimate_minutes: float = 20.0) -> bool:
    """True — можно работать (и минуты зарезервированы). False — бюджет исчерпан."""
    ado = _ado()
    if ado:
        wid = ado.budget_ledger(ACCOUNT, _month(), create=True)
        u = ado.budget_read(wid)
        if u + estimate_minutes > CAP:
            print(f"[budget] {ACCOUNT}: {u:.0f}/{CAP} мин — пропуск "
                  f"(нужно ещё ~{estimate_minutes:.0f})")
            return False
        ado.budget_add(wid, estimate_minutes)
        print(f"[budget] {ACCOUNT}: +{estimate_minutes:.0f} мин зарезервировано "
              f"(было {u:.0f}/{CAP}) [ADO]")
        return True
    u = _r2_used(ACCOUNT)
    if u + estimate_minutes > CAP:
        print(f"[budget] {ACCOUNT}: {u:.0f}/{CAP} мин — пропуск")
        return False
    _r2_write(u + estimate_minutes, ACCOUNT)
    print(f"[budget] {ACCOUNT}: +{estimate_minutes:.0f} мин (было {u:.0f}/{CAP}) [R2]")
    return True


def record(delta_minutes: float) -> None:
    """Уточнить бюджет по факту (delta = факт - оценка; может быть отрицательным)."""
    ado = _ado()
    if ado:
        wid = ado.budget_ledger(ACCOUNT, _month(), create=True)
        ado.budget_add(wid, delta_minutes)
        return
    _r2_write(max(0.0, _r2_used(ACCOUNT) + delta_minutes), ACCOUNT)
