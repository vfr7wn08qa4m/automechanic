"""Хранение результатов в несколько реплик.

1. JSONL-архив локально (data/cases.jsonl) — всегда, это «истина» на диске.
2. S3-совместимое хранилище (Cloudflare R2 free 10GB / Backblaze B2 free 10GB) —
   сырые титры + кейсы, если настроено.
3. Qdrant Cloud (free 1GB ≈ 200k кейсов при 1024d) — вектор + payload для поиска.

Oracle 23ai (AI Vector Search в Always Free Autonomous DB) — вторая поисковая
реплика, добавляется на фазе бекэнда (см. README, фаза 5).
"""
from __future__ import annotations

import hashlib
import json
import uuid

import requests

from . import config
from .case_schema import RepairCase

CASES_JSONL = config.DATA_DIR / "cases.jsonl"


def case_uuid(case: RepairCase) -> str:
    """Детерминированный id: повторная обработка видео перезапишет ту же точку."""
    key = case.source.video_id or case.source.url or case.problem_summary
    return str(uuid.UUID(hashlib.md5(f"case:{key}".encode()).hexdigest()))


def append_jsonl(case: RepairCase) -> None:
    with CASES_JSONL.open("a", encoding="utf-8") as f:
        f.write(case.model_dump_json() + "\n")


# --- S3-совместимый архив (R2 / B2) -------------------------------------------

def s3_client():
    import boto3
    return boto3.client("s3", endpoint_url=config.S3_ENDPOINT,
                        aws_access_key_id=config.S3_KEY,
                        aws_secret_access_key=config.S3_SECRET)


def archive_blob(key: str, body: str) -> str | None:
    """Положить артефакт (титры/кейс) в бакет. Возвращает s3-ключ или None."""
    if not (config.S3_ENDPOINT and config.S3_KEY and config.S3_SECRET):
        return None
    s3_client().put_object(Bucket=config.S3_BUCKET, Key=key,
                           Body=body.encode("utf-8"))
    return key


# --- Qdrant --------------------------------------------------------------------

def _qdrant_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.QDRANT_API_KEY:
        h["api-key"] = config.QDRANT_API_KEY
    return h


def qdrant_ensure_collection() -> None:
    url = f"{config.QDRANT_URL}/collections/{config.QDRANT_COLLECTION}"
    r = requests.get(url, headers=_qdrant_headers(), timeout=30)
    if r.status_code == 200:
        return
    requests.put(url, headers=_qdrant_headers(), timeout=30, json={
        "vectors": {"size": config.EMBED_DIM, "distance": "Cosine"},
    }).raise_for_status()


def qdrant_upsert(case: RepairCase, vector: list[float]) -> None:
    if not config.QDRANT_URL:
        return
    qdrant_ensure_collection()
    point = {
        "id": case_uuid(case),
        "vector": vector,
        "payload": {
            "url": case.source.url,
            "title": case.source.title,
            "channel": case.source.channel,
            "lang": case.lang,
            "make": case.vehicle.make,
            "model": case.vehicle.model,
            "system": case.system,
            "dtc_codes": case.dtc_codes,
            "symptoms": case.symptoms,
            "sounds": [f"{s.description} ({s.depends_on})".strip()
                       for s in case.sounds],
            "problem_summary": case.problem_summary,
            "root_cause": case.root_cause,
            "fixed": case.fixed,
            "applicability": case.applicability,
            "applicability_note": case.applicability_note,
            "summary_en": case.summary_en,
        },
    }
    r = requests.put(
        f"{config.QDRANT_URL}/collections/{config.QDRANT_COLLECTION}/points?wait=true",
        headers=_qdrant_headers(), json={"points": [point]}, timeout=60)
    r.raise_for_status()


def qdrant_search(vector: list[float], limit: int = 10,
                  flt: dict | None = None) -> list[dict]:
    body: dict = {"vector": vector, "limit": limit, "with_payload": True}
    if flt:
        body["filter"] = flt
    r = requests.post(
        f"{config.QDRANT_URL}/collections/{config.QDRANT_COLLECTION}/points/search",
        headers=_qdrant_headers(), json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("result", [])
