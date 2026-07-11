#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy.py — раскатка AutoMech по парным CircleCI-аккаунтам.

Единственный источник правды — accounts.json (локальный, секретный; НЕ в git).
Пример структуры лежит рядом с этим репо у оператора; поля описаны в его _README.

Действия (флаги можно комбинировать):
  --code     : код из текущей папки (без секретов и data/) -> force-push в
               code_repo КАЖДОГО заполненного аккаунта его github_token
               (коммит [skip ci], чтобы push сам по себе не жёг кредиты).
  --context  : через CircleCI API создаёт/находит контекст 'automech' в каждом
               аккаунте и заливает shared_secrets + ADO_ORG/ADO_PROJECT/ADO_PAT
               (из секции azure) + PARTITION (из аккаунта).
  --trigger  : дёргает тестовый пайплайн в каждом аккаунте (/pipeline/run) —
               проверка, что CircleCI просыпается.
  --check    : только проверить сроки токенов и выйти.
  (без флагов = --code + --context.)

Требуется git (для --code). Только stdlib.

Запуск:
    python deploy.py --accounts C:\\...\\scratchpad\\accounts.json --code
    python deploy.py --accounts <path> --context
    python deploy.py --accounts <path> --trigger
    python deploy.py --accounts <path>            # code + context
"""
from __future__ import annotations

import os
import sys
import json
import shutil
import datetime
import tempfile
import argparse
import subprocess
import urllib.request
import urllib.error

try:                              # UTF-8 даже в cp1251-консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

API = "https://circleci.com/api/v2"

# Что НЕ пушим в код-репо: секреты, локальные данные, мусор.
EXCLUDE_NAMES = {"accounts.json", ".git", ".github", "__pycache__", "data",
                 "state", ".idea", ".vscode", ".venv", "venv", ".env",
                 "keys.py", ".pytest_cache"}
EXCLUDE_EXT = {".zip", ".har", ".pyc", ".log", ".vtt", ".srt", ".json3"}
FORBIDDEN = ("accounts.json", ".env", "keys.py")   # двойная страховка от утечки

CONTEXT_NAME = "automech"


# ── загрузка / валидация ──────────────────────────────────────────────────────

def load_config(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    accts = data.get("accounts", [])
    if not isinstance(accts, list) or not accts:
        raise SystemExit(f"нет accounts в {path}")
    az = data.get("azure") or {}
    secrets = data.get("shared_secrets") or {}
    return accts, az, secrets


def _parse_date(s):
    s = str(s).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


NO_EXPIRY = {"", "none", "never", "no", "-", "n/a", "никогда", "бессрочно"}


def check_expiries(accts, az, warn_days=14):
    today = datetime.date.today()
    soon = today + datetime.timedelta(days=warn_days)
    rows = []
    for a in accts:
        for f in ("github_token_expires", "circleci_token_expires"):
            rows.append((f"{a.get('name','?')}.{f}", a.get(f)))
    rows.append(("azure.pat_expires", az.get("pat_expires")))

    problems = False
    for label, raw in rows:
        if str(raw).strip().lower() in NO_EXPIRY:
            continue
        d = _parse_date(raw)
        if d is None:
            print(f"  ! {label}: неразобранная дата '{raw}'", file=sys.stderr)
            problems = True
        elif d < today:
            print(f"  ! {label}: ПРОСРОЧЕН ({d})", file=sys.stderr)
            problems = True
        elif d < soon:
            print(f"  ~ {label}: истекает скоро ({d})", file=sys.stderr)
    if not problems:
        print("# сроки токенов в порядке")
    return not problems


def _owner_slug(a):
    """owner-slug для CircleCI-контекста: явный или gh/<owner из code_repo>."""
    if a.get("circleci_owner_slug"):
        return a["circleci_owner_slug"].strip()
    repo = str(a.get("code_repo", "")).strip()
    owner = repo.split("/")[0] if "/" in repo else ""
    return f"gh/{owner}" if owner else ""


# ── HTTP к CircleCI ───────────────────────────────────────────────────────────

def _api(url, token, method="GET", body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Circle-Token": token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt.strip() else {}


# ── раскатка кода ─────────────────────────────────────────────────────────────

def _git(args, cwd, env):
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def stage_code(src_dir):
    tmp = tempfile.mkdtemp(prefix="automech_deploy_")
    for name in os.listdir(src_dir):
        if name in EXCLUDE_NAMES or name.startswith("_"):
            continue
        if os.path.splitext(name)[1].lower() in EXCLUDE_EXT:
            continue
        s = os.path.join(src_dir, name)
        d = os.path.join(tmp, name)
        if os.path.isdir(s):
            shutil.copytree(s, d,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                          ".env", "keys.py"))
        else:
            shutil.copy2(s, d)
    for f in FORBIDDEN:
        if os.path.exists(os.path.join(tmp, f)):
            shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit(f"ОТКАЗ: {f} попал в стейджинг — пуш отменён")
    return tmp


def push_code(accts, src_dir, branch="main"):
    ready = [a for a in accts
             if a.get("github_token") and "REPLACE" not in str(a["github_token"])
             and len(str(a.get("code_repo", "")).split("/")) == 2
             and all(str(a["code_repo"]).split("/"))]
    if not ready:
        sys.exit("нет аккаунтов с github_token + code_repo — заполни хотя бы один")
    print(f"# к пушу готовы: {', '.join(a.get('name','?') for a in ready)}")

    staged = stage_code(src_dir)
    items = sorted(os.listdir(staged))
    print(f"# staged {len(items)} items: {', '.join(items[:14])}"
          + (" …" if len(items) > 14 else ""))
    try:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        _git(["init", "-q"], staged, env)
        _git(["checkout", "-q", "-B", branch], staged, env)
        _git(["add", "-A"], staged, env)
        _git(["-c", "user.name=deploy", "-c", "user.email=deploy@local",
              "commit", "-q", "-m", "deploy code [skip ci]"], staged, env)
        for a in ready:
            repo, tok = a["code_repo"], a["github_token"]
            url = f"https://x-access-token:{tok}@github.com/{repo}.git"
            print(f"# push code -> {repo}")
            try:
                _git(["push", "--force", url, f"{branch}:{branch}"], staged, env)
                print(f"  ok: {repo}")
            except subprocess.CalledProcessError as e:
                # НЕ печатаем e — в аргументах URL с токеном.
                print(f"  ! push failed {repo} (rc {e.returncode}) — проверь scope "
                      f"токена 'repo' и что репо существует", file=sys.stderr)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


# ── контекст automech + секреты ───────────────────────────────────────────────

def _find_or_create_context(owner_slug, token):
    """Вернуть id контекста CONTEXT_NAME для owner_slug, создав при отсутствии."""
    try:
        items = _api(f"{API}/context?owner-slug={owner_slug}", token).get("items", [])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"список контекстов ({owner_slug}) -> {e.code}: "
                           f"{e.read().decode('utf-8','replace')[:200]}")
    for it in items:
        if it.get("name") == CONTEXT_NAME:
            return it["id"]
    body = {"name": CONTEXT_NAME,
            "owner": {"slug": owner_slug, "type": "organization"}}
    try:
        return _api(f"{API}/context", token, method="POST", body=body)["id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"не удалось создать контекст '{CONTEXT_NAME}' у {owner_slug} -> "
            f"{e.code}: {e.read().decode('utf-8','replace')[:200]}. "
            f"Создай контекст '{CONTEXT_NAME}' в UI (Organization Settings -> "
            f"Contexts) и повтори — переменные зальются в существующий.")


def _put_ctx_var(ctx_id, token, name, value):
    body = {"value": str(value)}
    url = f"{API}/context/{ctx_id}/environment-variable/{name}"
    try:
        _api(url, token, method="PUT", body=body)
        return True
    except urllib.error.HTTPError as e:
        print(f"    ! {name}: {e.code} {e.read().decode('utf-8','replace')[:120]}",
              file=sys.stderr)
        return False


def set_context(accts, az, secrets):
    # Итоговый набор переменных = shared_secrets + ADO из azure. Пустые пропускаем.
    base = {k: v for k, v in secrets.items() if str(v).strip()}
    if az.get("org"):
        base["ADO_ORG"] = az["org"]
    if az.get("project"):
        base["ADO_PROJECT"] = az["project"]
    if az.get("pat"):
        base["ADO_PAT"] = az["pat"]

    for a in accts:
        tok = a.get("circleci_token")
        if not tok or "REPLACE" in str(tok):
            print(f"# пропуск {a.get('name')} — circleci_token не заполнен")
            continue
        owner = _owner_slug(a)
        if not owner:
            print(f"# пропуск {a.get('name')} — не вывести owner-slug "
                  f"(заполни code_repo или circleci_owner_slug)", file=sys.stderr)
            continue
        print(f"# контекст '{CONTEXT_NAME}' @ {owner}  ({a.get('name')})")
        try:
            ctx_id = _find_or_create_context(owner, tok)
        except RuntimeError as e:
            print(f"  ! {e}", file=sys.stderr)
            continue
        vars_here = dict(base)
        ok = sum(_put_ctx_var(ctx_id, tok, n, v) for n, v in vars_here.items())
        print(f"  залито переменных: {ok}/{len(vars_here)}")


# ── тестовый триггер ──────────────────────────────────────────────────────────

def _definition_id(slug, token):
    try:
        uuid = _api(f"{API}/project/{slug}", token).get("id")
        if not uuid:
            return None
        items = _api(f"{API}/projects/{uuid}/pipeline-definitions",
                     token).get("items", [])
    except Exception:
        return None
    return items[0]["id"] if items else None


def trigger(accts, branch="main"):
    for a in accts:
        tok, slug = a.get("circleci_token"), a.get("circleci_project_slug")
        if not tok or not slug or "REPLACE" in str(tok):
            print(f"# пропуск {a.get('name')} — нет circleci_token/slug")
            continue
        defn = a.get("circleci_definition_id") or _definition_id(slug, tok)
        params = {"batch-size": 2}          # партиций нет: очередь + атомарный клейм
        try:
            if defn:
                body = {"definition_id": defn, "config": {"branch": branch},
                        "checkout": {"branch": branch}, "parameters": params}
                res = _api(f"{API}/project/{slug}/pipeline/run", tok,
                           method="POST", body=body)
            else:                      # классические проекты gh/<owner>/<repo>
                body = {"branch": branch, "parameters": params}
                res = _api(f"{API}/project/{slug}/pipeline", tok,
                           method="POST", body=body)
        except urllib.error.HTTPError as e:
            print(f"  ! {a.get('name')}: {e.code} "
                  f"{e.read().decode('utf-8','replace')[:200]}", file=sys.stderr)
            continue
        print(f"# {a.get('name')}: queued pipeline #{res.get('number')} "
              f"(state {res.get('state')})")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Раскатка AutoMech по CircleCI-аккаунтам")
    ap.add_argument("--accounts", required=True, help="путь к accounts.json (секретный)")
    ap.add_argument("--code", action="store_true", help="запушить код в code_repo")
    ap.add_argument("--context", action="store_true", help="контекст automech + секреты")
    ap.add_argument("--trigger", action="store_true", help="дёрнуть тестовый пайплайн")
    ap.add_argument("--check", action="store_true", help="только проверить сроки токенов")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    accts, az, secrets = load_config(args.accounts)
    src_dir = os.path.dirname(os.path.abspath(__file__))

    print("== проверка сроков ==")
    check_expiries(accts, az)
    if args.check:
        return

    do_code = args.code or not (args.context or args.trigger)
    do_ctx = args.context or not (args.code or args.trigger)

    if do_code:
        print("\n== раскатка кода ==")
        push_code(accts, src_dir, args.branch)
    if do_ctx:
        print("\n== контекст automech ==")
        set_context(accts, az, secrets)
    if args.trigger:
        print("\n== тестовый триггер ==")
        trigger(accts, args.branch)


if __name__ == "__main__":
    main()
