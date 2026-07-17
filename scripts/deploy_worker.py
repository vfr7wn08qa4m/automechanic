#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Деплой Cloudflare Worker-реле (proxy/worker.js) через API — по токену из
accounts.json (секция cloudflare). Никаких кликов в дашборде.

Делает: заливает module-worker, включает <name>.<sub>.workers.dev, ставит секрет
PROXY_SECRET, и вписывает CRAWL_PROXY/CRAWL_PROXY_KEY в shared_secrets пульта
(дальше `deploy.py --context` разольёт их в CI-аккаунты).

    python scripts/deploy_worker.py --accounts accounts.json
Токен: шаблон «Edit Cloudflare Workers» (My Profile -> API Tokens).
"""
from __future__ import annotations

import argparse
import json
import secrets as _secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"


def _req(method: str, path: str, token: str, body: bytes | None = None,
         content_type: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(API + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"CF API {method} {path} -> {e.code}: {detail[:400]}")


def _multipart_worker(js: str) -> tuple[bytes, str]:
    """multipart/form-data для module-воркера (metadata + worker.js)."""
    boundary = "----automech" + _secrets.token_hex(8)
    meta = json.dumps({"main_module": "worker.js",
                       "compatibility_date": "2025-01-01"})
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"; '
        f'filename="metadata.json"\r\nContent-Type: application/json\r\n\r\n{meta}\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="worker.js"; '
        f'filename="worker.js"\r\nContent-Type: application/javascript+module\r\n\r\n{js}\r\n',
        f'--{boundary}--\r\n',
    ]
    return "".join(parts).encode("utf-8"), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="accounts.json")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    cfg = json.loads((root / args.accounts).read_text(encoding="utf-8"))
    cf = cfg.get("cloudflare") or {}
    token = cf.get("api_token", "").strip()
    if not token:
        raise SystemExit("cloudflare.api_token пуст — вставь токен 'Edit Cloudflare Workers'")
    name = cf.get("worker_name") or "automech-relay"
    secret = cf.get("proxy_secret") or _secrets.token_urlsafe(24)

    # 1) account_id
    aid = cf.get("account_id", "").strip()
    if not aid:
        accs = _req("GET", "/accounts", token).get("result", [])
        if not accs:
            raise SystemExit("токен не видит ни одного аккаунта")
        aid = accs[0]["id"]
        print(f"# account_id: {aid} ({accs[0].get('name','')})")

    # 2) workers.dev субдомен
    sub = _req("GET", f"/accounts/{aid}/workers/subdomain", token).get("result", {}).get("subdomain")
    if not sub:
        raise SystemExit("у аккаунта нет workers.dev субдомена — зайди раз в "
                         "dash -> Workers & Pages, он создастся, и повтори")
    print(f"# субдомен: {sub}.workers.dev")

    # 3) залить воркер
    js = (root / "proxy" / "worker.js").read_text(encoding="utf-8")
    body, ctype = _multipart_worker(js)
    _req("PUT", f"/accounts/{aid}/workers/scripts/{name}", token, body, ctype)
    print(f"# воркер '{name}' залит")

    # 4) включить <name>.<sub>.workers.dev
    _req("POST", f"/accounts/{aid}/workers/scripts/{name}/subdomain", token,
         json.dumps({"enabled": True}).encode(), "application/json")
    # 5) секрет PROXY_SECRET
    _req("PUT", f"/accounts/{aid}/workers/scripts/{name}/secrets", token,
         json.dumps({"name": "PROXY_SECRET", "text": secret,
                     "type": "secret_text"}).encode(), "application/json")
    url = f"https://{name}.{sub}.workers.dev"
    print(f"# готово: {url}  (секрет PROXY_SECRET установлен)")

    # 6) вписать в пульт: cloudflare.* + shared_secrets (для --context)
    cf.update(account_id=aid, proxy_secret=secret, worker_url=url)
    cfg["cloudflare"] = cf
    cfg.setdefault("shared_secrets", {})["CRAWL_PROXY"] = url
    cfg["shared_secrets"]["CRAWL_PROXY_KEY"] = secret
    (root / args.accounts).write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print("# accounts.json обновлён: cloudflare.worker_url + shared_secrets.CRAWL_PROXY[_KEY]")
    print(f"\nПроверка вручную:\n  {url}/?url=https%3A%2F%2Fbobistheoilguy.com%2Fforums%2F&k={secret}")


if __name__ == "__main__":
    main()
