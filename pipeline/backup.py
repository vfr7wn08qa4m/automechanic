"""Дневной бэкап ADO -> GitHub: реестр эпиков-источников (каналы/форумы) +
готовые после эмбеда (indexed) воркайтемы с телом (RepairCase + транскрипт).

Раз в сутки один снапшот `backups/<YYYY-MM-DD>.ndjson` через GitHub Contents API
(без клона). Идемпотентность = гард «раз/сутки»: если файл за сегодня уже есть —
задача пропускается (это и есть маркер последнего бэкапа, отдельный стейт не нужен).

Репо + токен из env (кладёт deploy.py в контекст 'automech'):
    BACKUP_REPO           — owner/name репо-приёмника
    BACKUP_GITHUB_TOKEN   — GitHub PAT (scope repo) с write в него
    BACKUP_BRANCH         — ветка (по умолчанию main)
Нет env -> бэкап тихо выключен (run_backup вернёт False).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

GH = "https://api.github.com"


def _env(n: str) -> str | None:
    v = os.getenv(n)
    return v if v and v.strip() else None


def _gh(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{GH}{path}", data=data, method=method,
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "automech-backup"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def _exists(repo: str, token: str, path: str, branch: str) -> bool:
    try:
        _gh("GET", f"/repos/{repo}/contents/{path}?ref={branch}", token)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def run_backup(ado, *, force: bool = False) -> bool:
    """Сделать дневной снапшот, если сегодня его ещё не было. True — сделали."""
    repo, token = _env("BACKUP_REPO"), _env("BACKUP_GITHUB_TOKEN")
    if not (repo and token):
        print("[backup] BACKUP_REPO/BACKUP_GITHUB_TOKEN не заданы — бэкап выключен")
        return False
    branch = _env("BACKUP_BRANCH") or "main"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = f"backups/{day}.ndjson"

    if not force and _exists(repo, token, path, branch):
        print(f"[backup] за {day} снапшот уже есть ({path}) — пропуск (раз/сутки)")
        return False

    lines: list[str] = []
    # реестр источников: эпики каналов и форумов (активные и на паузе)
    epics = (ado.list_channel_items(kind="channel", active_only=False)
             + ado.list_channel_items(kind="forum", active_only=False))
    for e in epics:
        lines.append(json.dumps({"type": "epic", **e}, ensure_ascii=False))
    # готовые воркайтемы (indexed) с телом тикета (там RepairCase + транскрипт)
    ids = ado.query_by_state("indexed", top=100000)
    fields = ("System.Title", "System.Tags", "System.State", "System.Description")
    for wi in ado.get_batch(ids, fields=fields):
        f = wi.get("fields", {})
        lines.append(json.dumps({
            "type": "workitem", "id": wi.get("id"),
            "title": f.get("System.Title"), "tags": f.get("System.Tags"),
            "state": f.get("System.State"), "description": f.get("System.Description"),
        }, ensure_ascii=False))

    blob = ("\n".join(lines) + "\n").encode("utf-8")
    print(f"[backup] {day}: эпиков={len(epics)} indexed={len(ids)} "
          f"размер={len(blob)//1024} КБ")
    if len(blob) > 45 * 1024 * 1024:
        print("[backup] ! снапшот >45 МБ — Contents API может отказать; "
              "нужен сплит по частям или Git Data API (доработать)")

    payload = {"message": f"backup {day} [skip ci]",
               "content": base64.b64encode(blob).decode(), "branch": branch}
    try:
        _gh("PUT", f"/repos/{repo}/contents/{path}", token, payload)
        print(f"[backup] залито -> {repo}/{path}")
        return True
    except urllib.error.HTTPError as e:
        print(f"[backup] ! PUT {e.code}: {e.read().decode('utf-8','replace')[:200]}")
        return False
