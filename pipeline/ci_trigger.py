"""Кольцо конвейера: 8 аккаунтов GitHub в круговой цепочке.
gh1→gh2→...→gh8→gh1. Каждый аккаунт запускает следующий через GitHub API.

Статическое кольцо (захардкодено в коде):
    Читаем ghtockens.txt (8 токенов для gh1-gh8)
    Каждый аккаунт запускает следующего по кругу

RING_SIZE = 8 — число подключённых аккаунтов (порог стопа)
RING_IDLE — сколько аккаунтов ПОДРЯД прошли вхолостую
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


def _env(name: str) -> str | None:
    v = os.getenv(name)
    return v if v and v.strip() else None


def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(_env(name) or default)
    except (TypeError, ValueError):
        return default


def _load_ring_tokens() -> list[str]:
    """Загрузить токены кольца из ghtockens.txt (8 аккаунтов)."""
    try:
        tokens_file = Path("ghtockens.txt")
        if not tokens_file.exists():
            tokens_file = Path(__file__).parent.parent / "ghtockens.txt"
        if tokens_file.exists():
            with open(tokens_file) as f:
                return [line.strip() for line in f if line.strip()]
    except Exception:
        pass
    return []


def _get_current_token_index(current_token: str) -> int:
    """Найти индекс текущего токена в кольце."""
    tokens = _load_ring_tokens()
    if not tokens:
        return -1

    for i, token in enumerate(tokens):
        try:
            req = urllib.request.Request(
                "https://api.github.com/user",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                user_info = json.load(resp)
                current_user = _get_user_for_token(current_token)
                if user_info.get("login") == current_user:
                    return i
        except Exception:
            pass

    return -1


def _get_user_for_token(token: str) -> str | None:
    """Получить username для токена."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            user_info = json.load(resp)
            login = user_info.get("login")
            return login
    except Exception as e:
        print(f"[ring] ERROR in _get_user_for_token: {type(e).__name__}: {str(e)[:80]}")
        return None


def _get_next_token() -> str | None:
    """Получить токен следующего аккаунта в кольце."""
    tokens = _load_ring_tokens()
    if len(tokens) < 2:
        print("[ring] WARNING: less than 2 tokens loaded")
        return None

    # Сначала пробуем получить текущий токен из переменных (если явно передан)
    current_token = os.getenv("CURRENT_GITHUB_TOKEN") or _env("CURRENT_GITHUB_TOKEN")

    # Если не передан явно, пробуем угадать по github.actor (username)
    if not current_token:
        current_actor = os.getenv("GITHUB_ACTOR")
        print(f"[ring] GITHUB_ACTOR={current_actor}")
        if current_actor:
            # Пройтись по токенам и найти тот, который принадлежит current_actor
            for i, token in enumerate(tokens):
                user = _get_user_for_token(token)
                print(f"[ring]   token[{i}] -> user={user}")
                if user == current_actor:
                    current_token = token
                    print(f"[ring]   MATCHED token[{i}]!")
                    break
        if not current_token:
            print(f"[ring] WARNING: No matching token found for {current_actor}")

    # Фолбэк: если всё ещё нет, попробуем встроенный GITHUB_TOKEN
    if not current_token:
        current_token = os.getenv("GITHUB_TOKEN") or _env("GITHUB_TOKEN")
        print(f"[ring] Fallback to GITHUB_TOKEN (present={bool(current_token)})")

    if not current_token:
        print("[ring] CRITICAL: no current_token found!")
        return None

    current_idx = _get_current_token_index(current_token)
    print(f"[ring] current_idx={current_idx}")
    if current_idx == -1:
        print("[ring] WARNING: current_idx=-1, token not in ring?")
        return None

    next_idx = (current_idx + 1) % len(tokens)
    next_token = tokens[next_idx]
    print(f"[ring] next_idx={next_idx}, returning token for account {next_idx+1}")
    return next_token


def can_ring() -> bool:
    """Кольцо: есть токены И файл .ring-stop не существует (off-switch).
    Для остановки кольца создать файл .ring-stop в репо."""
    # Off-switch: если существует файл .ring-stop, кольцо не работает
    if Path(".ring-stop").exists():
        print("[ring] файл .ring-stop найден - кольцо остановлено")
        return False
    return _get_next_token() is not None


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


def trigger(flow: str, *, partition: str | None = None, zone: str | None = None,
            batch: int | None = None, idle: int | None = None) -> str | None:
    """Запустить пайплайн СЛЕДУЮЩЕГО аккаунта в кольце."""

    print(f"[ring] trigger() called: flow={flow}, partition={partition}, batch={batch}")

    if not can_ring():
        print("[ring] FAIL: can_ring()=False - stopping trigger")
        return None

    next_token = _get_next_token()
    if not next_token:
        print("[ring] FAIL: _get_next_token() returned None - no next token?")
        return None
    print(f"[ring] OK: next_token found ({next_token[:12]}...)")

    next_user = _get_user_for_token(next_token)
    if not next_user:
        print("[ring] FAIL: _get_user_for_token(next_token) returned None")
        return None
    print(f"[ring] OK: next_user={next_user}")

    inputs: dict = {}
    if partition:
        inputs["partition"] = partition
    if zone:
        inputs["zone"] = zone
    if batch:
        inputs["batch"] = str(batch)
    if idle is not None:
        inputs["idle"] = str(idle)

    print(f"[ring] Calling _run_github_workflow for {next_user}/automechanic...")
    result = _run_github_workflow(next_token, f"{next_user}/automechanic", "tick.yml",
                               inputs, "ring")
    print(f"[ring] _run_github_workflow returned: {result}")
    return result


def ring_handoff(flow: str, *, worked: bool, partition: str | None = None,
                 zone: str | None = None, batch: int | None = None) -> str | None:
    """Единая точка передачи эстафеты следующему аккаунту в кольце.

    worked=True  — этот аккаунт реально поработал: idle сбрасывается в 0.
    worked=False — пропустил тик (бюджет исчерпан / нет работы): idle+1.
                   Эстафета всё равно уходит дальше — упёршийся аккаунт кольцо не рвёт.
    Кольцо останавливается, когда полный круг прошёл вхолостую (idle >= RING_SIZE) —
    значит очередь ADO разгребена. Защита от вечного холостого кручения.
    """
    print(f"[ring] ring_handoff called: worked={worked}")
    print(f"[ring] can_ring()={can_ring()}")

    if can_ring():
        size = _int_env("RING_SIZE", 8)
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
