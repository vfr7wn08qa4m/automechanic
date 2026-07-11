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
    url = (f"https://api.cloudflare.com/client/v4/accounts/"
           f"{config.CF_ACCOUNT_ID}/ai/run/{config.CF_EMBED_MODEL}")
    r = requests.post(url, json={"text": texts},
                      headers={"Authorization": f"Bearer {config.CF_API_TOKEN}"},
                      timeout=60)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"cloudflare ai error: {body.get('errors')}")
    return body["result"]["data"]


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
