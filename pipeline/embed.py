"""Эмбеддинги bge-m3 (1024d, мультиязычная).

Порядок провайдеров (проверено 2026-07-10):
1. NVIDIA NIM — если задан EMBED_API_KEY. Внимание: baai/bge-m3 на NIM сейчас
   отдаёт 500; живая альтернатива nvidia/nv-embedqa-e5-v5 (1024d), но она
   англо-центричная — для мультиязычного продукта не подходит как основная.
2. Cloudflare Workers AI @cf/baai/bge-m3 — те же веса и 1024d.
3. Локально sentence-transformers BAAI/bge-m3 (та же модель) — на билд-агенте,
   без лимитов:  pip install -r requirements-embed-local.txt

Все три источника дают совместимые векторы -> один индекс, взаимные реплики.
"""
from __future__ import annotations

import requests
from openai import OpenAI

from . import config


def _load_cf_accounts() -> list[tuple[str, str]]:
    """Загрузить (CF_ACCOUNT_ID, CF_API_TOKEN) пары для ротации.

    Порядок приоритета:
    1. env CF_TOKENS (формат: "id1;token1;id2;token2;...")
    2. env CF_ACCOUNTS (формат: "id1|token1;id2|token2;...")
    3. cf_tokens.txt (формат: CF_ACCOUNT_ID_N=... CF_API_TOKEN_N=...)
    4. config (CF_ACCOUNT_ID + CF_API_TOKEN, один аккаунт)
    """
    import os
    from pathlib import Path

    accounts = []

    # 1. Try env CF_TOKENS (semicolon-separated tokens: id1;token1;id2;token2;...)
    env_cf_tokens = os.getenv("CF_TOKENS", "").strip()
    if env_cf_tokens:
        tokens_list = [t.strip() for t in env_cf_tokens.split(";") if t.strip()]
        # Pair them up: id1, token1, id2, token2, ...
        for i in range(0, len(tokens_list) - 1, 2):
            account_id, token = tokens_list[i], tokens_list[i + 1]
            if account_id and token:
                accounts.append((account_id, token))
        if accounts:
            return accounts

    # 2. Try env CF_ACCOUNTS (semicolon-separated pairs)
    env_accounts = os.getenv("CF_ACCOUNTS", "").strip()
    if env_accounts:
        for pair in env_accounts.split(";"):
            parts = pair.strip().split("|")
            if len(parts) == 2:
                account_id, token = parts[0].strip(), parts[1].strip()
                if account_id and token:
                    accounts.append((account_id, token))
        if accounts:
            return accounts

    # 3. Try cf_tokens.txt (переменные CF_ACCOUNT_ID_N и CF_API_TOKEN_N)
    cf_file = Path(__file__).parent.parent / "cf_tokens.txt"
    if cf_file.exists():
        # encoding ОБЯЗАТЕЛЕН: без него Windows читает в cp1252 и падает с
        # UnicodeDecodeError на русских комментариях в файле — из-за этого весь
        # пул CF-аккаунтов локально не загружался (в Linux-кольце везло на UTF-8).
        content = cf_file.read_text(encoding="utf-8", errors="replace")
        i = 1
        while True:
            account_id = None
            token = None
            for line in content.split("\n"):
                if line.startswith(f"CF_ACCOUNT_ID_{i}="):
                    account_id = line.split("=", 1)[1].strip()
                elif line.startswith(f"CF_API_TOKEN_{i}="):
                    token = line.split("=", 1)[1].strip()
            if account_id and token:
                accounts.append((account_id, token))
                i += 1
            else:
                break

    # 3. Fallback to config (один аккаунт)
    if not accounts and config.CF_ACCOUNT_ID and config.CF_API_TOKEN:
        accounts.append((config.CF_ACCOUNT_ID, config.CF_API_TOKEN))

    return accounts


def _nim(texts: list[str], input_type: str) -> list[list[float]]:
    client = OpenAI(api_key=config.EMBED_API_KEY, base_url=config.EMBED_BASE_URL)
    resp = client.embeddings.create(
        input=texts,
        model=config.EMBED_MODEL,
        encoding_format="float",
        extra_body={"input_type": input_type, "truncate": "END"},
    )
    return [d.embedding for d in resp.data]


def _cloudflare(texts: list[str]) -> list[list[float]]:
    accounts = _load_cf_accounts()
    if not accounts:
        raise RuntimeError("CF аккаунты не найдены (ни в cf_tokens.txt, ни в env, ни в config)")

    errors = []
    for i, (account_id, token) in enumerate(accounts, 1):
        if not account_id or not token:
            errors.append(f"account #{i}: не заполнены")
            continue

        try:
            url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{config.CF_EMBED_MODEL}"
            r = requests.post(url, json={"text": texts},
                              headers={"Authorization": f"Bearer {token}"},
                              timeout=60)
            r.raise_for_status()
            body = r.json()
            if not body.get("success"):
                err = body.get("errors")
                status = body.get("errors", [{}])[0].get("code")
                errors.append(f"account #{i}: код {status}")
                # 10027 = quota exceeded, 10000 = auth error → пробуем следующий
                if status in (10027, 10000):
                    continue
                raise RuntimeError(f"cloudflare ai error: {err}")
            return body["result"]["data"]
        except requests.exceptions.RequestException as e:
            status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            errors.append(f"account #{i}: HTTP {status or 'error'}")
            # 401/429 → пробуем следующий аккаунт
            if status in (401, 429):
                continue
            raise
    raise RuntimeError("cloudflare: все аккаунты недоступны: " + " | ".join(errors))


_local_model = None


def _local(texts: list[str]) -> list[list[float]]:
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer
        _local_model = SentenceTransformer("BAAI/bge-m3")
    return _local_model.encode(texts, normalize_embeddings=True).tolist()


def _remote(texts: list[str], input_type: str) -> list[list[float]]:
    """Свой сервер (Kaggle T4 и т.п.): POST /embed {"texts": [...], "input_type": ...}."""
    headers = {"Content-Type": "application/json"}
    if config.EMBED_REMOTE_KEY:
        headers["X-Api-Key"] = config.EMBED_REMOTE_KEY
    r = requests.post(f"{config.EMBED_REMOTE_URL.rstrip('/')}/embed",
                      json={"texts": texts, "input_type": input_type},
                      headers=headers, timeout=120)
    r.raise_for_status()
    return r.json()["vectors"]


def _check_cf_quota() -> tuple[bool, str]:
    """Проверить, есть ли хотя бы один доступный CF аккаунт (не исчерпана ли квота на всех)."""
    accounts = _load_cf_accounts()
    if not accounts:
        return True, "no CF accounts configured, skipping quota check"

    for i, (account_id, token) in enumerate(accounts, 1):
        if not account_id or not token:
            continue  # пропускаем незаполненные слоты

        try:
            url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{config.CF_EMBED_MODEL}"
            r = requests.post(url, json={"text": ["test"]},
                              headers={"Authorization": f"Bearer {token}"},
                              timeout=10)
            body = r.json()
            # Если успешно — у этого аккаунта есть квота
            if body.get("success"):
                return True, f"quota available (account #{i})"
            # Если 10027 — квота исчерпана
            errors = body.get("errors", [])
            if any(e.get("code") == 10027 for e in errors):
                continue  # пробуем следующий
            # Другая ошибка (10000 auth, etc) — может быть не настроено, но не критично
            return True, f"account #{i}: unclear status, assuming ok"
        except requests.exceptions.RequestException as e:
            # Сетевая ошибка, не критично
            continue
    # Все аккаунты либо исчерпаны, либо незаполнены
    return False, "all CF accounts have quota exceeded or are not configured"


def embed(texts: list[str], input_type: str = "passage") -> list[list[float]]:
    """input_type: 'passage' для документов, 'query' для поисковых запросов."""
    errors = []
    if config.EMBED_REMOTE_URL:
        try:
            return _remote(texts, input_type)
        except Exception as e:  # noqa: BLE001
            errors.append(f"remote: {e}")
    if config.EMBED_API_KEY:
        try:
            return _nim(texts, input_type)
        except Exception as e:  # noqa: BLE001
            errors.append(f"nim: {e}")
    if config.CF_ACCOUNT_ID and config.CF_API_TOKEN:
        try:
            return _cloudflare(texts)
        except Exception as e:  # noqa: BLE001
            errors.append(f"cloudflare: {e}")
    try:
        return _local(texts)
    except ImportError:
        errors.append("local: sentence-transformers не установлен "
                      "(pip install -r requirements-embed-local.txt)")
    except Exception as e:  # noqa: BLE001
        errors.append(f"local: {e}")
    raise RuntimeError("все провайдеры эмбеддингов недоступны: " + "; ".join(errors))
