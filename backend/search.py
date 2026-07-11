"""Поиск кейсов: Qdrant (вектор bge-m3) + локальный фолбэк по cases.jsonl.

Фолбэк — простой лексический скоринг (пересечение токенов) — нужен, чтобы
бекэнд работал сразу, до настройки Qdrant/эмбеддингов, и как аварийный режим.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config                     # noqa: E402
from pipeline.case_schema import RepairCase     # noqa: E402

_TOKEN_RE = re.compile(r"[\w\-]{2,}", re.UNICODE)
_cases_cache: tuple[float, list[RepairCase]] | None = None


def load_cases() -> list[RepairCase]:
    """Кейсы из data/cases.jsonl с кэшем по mtime (для фолбэка и /case/{id})."""
    global _cases_cache
    f = config.DATA_DIR / "cases.jsonl"
    if not f.exists():
        return []
    mtime = f.stat().st_mtime
    if _cases_cache and _cases_cache[0] == mtime:
        return _cases_cache[1]
    cases = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                cases.append(RepairCase.model_validate_json(line))
            except Exception:  # noqa: BLE001 — битую строку пропускаем
                pass
    _cases_cache = (mtime, cases)
    return cases


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _passes_filters(payload: dict, flt: dict) -> bool:
    """Фильтр с учётом ОБЛАСТИ ПРИМЕНИМОСТИ: универсальные знания и знания
    уровня «весь тип двигателя»/«вся марка» не отсекаются фильтром по машине.
    Пример: фильтр make=Volkswagen пропустит и кейс «любой дизель: завоздушен
    ТНВД -> стравить воздух» (applicability=engine_type)."""
    appl = str(payload.get("applicability", "model")).lower()

    want_make = (flt.get("make") or "").strip().lower()
    if want_make and want_make not in str(payload.get("make", "")).lower():
        if appl not in ("engine_type", "universal"):
            return False
    want_model = (flt.get("model") or "").strip().lower()
    if want_model and want_model not in str(payload.get("model", "")).lower():
        if appl not in ("make", "engine_type", "universal"):
            return False

    for key in ("system", "lang"):
        want = (flt.get(key) or "").strip().lower()
        if want and want not in str(payload.get(key, "")).lower():
            return False
    want_dtc = (flt.get("dtc") or "").strip().upper()
    if want_dtc and want_dtc not in [d.upper() for d in payload.get("dtc_codes", [])]:
        return False
    return True


def _payload(case: RepairCase) -> dict:
    from pipeline.store import case_uuid
    return {
        "id": case_uuid(case),
        "url": case.source.url,
        "title": case.source.title,
        "channel": case.source.channel,
        "lang": case.lang,
        "make": case.vehicle.make,
        "model": case.vehicle.model,
        "system": case.system,
        "dtc_codes": case.dtc_codes,
        "symptoms": case.symptoms,
        "sounds": [f"{s.description} ({s.depends_on})".strip() for s in case.sounds],
        "problem_summary": case.problem_summary,
        "root_cause": case.root_cause,
        "fixed": case.fixed,
        "applicability": case.applicability,
        "applicability_note": case.applicability_note,
        "summary_en": case.summary_en,
    }


def local_search(query: str, flt: dict, limit: int) -> list[dict]:
    q = _tokens(query)
    if not q:
        return []
    scored = []
    for case in load_cases():
        if case.off_topic:
            continue
        p = _payload(case)
        if not _passes_filters(p, flt):
            continue
        doc = _tokens(case.search_text())
        overlap = len(q & doc)
        if overlap:
            scored.append({"score": round(overlap / len(q), 3), "payload": p,
                           "mode": "local"})
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def vector_search(query: str, flt: dict, limit: int) -> list[dict]:
    from pipeline.embed import embed
    from pipeline.store import qdrant_search
    vec = embed([query], input_type="query")[0]
    conditions: list[dict] = []
    # фильтры по машине — с учётом применимости (universal/engine_type проходят)
    if flt.get("make"):
        conditions.append({"should": [
            {"key": "make", "match": {"text": flt["make"]}},
            {"key": "applicability", "match": {"any": ["engine_type", "universal"]}},
        ]})
    if flt.get("model"):
        conditions.append({"should": [
            {"key": "model", "match": {"text": flt["model"]}},
            {"key": "applicability",
             "match": {"any": ["make", "engine_type", "universal"]}},
        ]})
    for key in ("system", "lang"):
        if flt.get(key):
            conditions.append({"key": key, "match": {"text": flt[key]}})
    if flt.get("dtc"):
        conditions.append({"key": "dtc_codes", "match": {"any": [flt["dtc"].upper()]}})
    qflt = {"must": conditions} if conditions else None
    hits = qdrant_search(vec, limit=limit, flt=qflt)
    return [{"score": round(h.get("score", 0), 3), "payload": h.get("payload", {}),
             "mode": "vector"} for h in hits]


def search(query: str, flt: dict | None = None, limit: int = 10) -> list[dict]:
    flt = flt or {}
    if config.QDRANT_URL:
        try:
            return vector_search(query, flt, limit)
        except Exception as e:  # noqa: BLE001 — вектор лёг, работаем лексически
            print(f"vector search failed, fallback to local: {e}")
    return local_search(query, flt, limit)


def get_case(case_id: str) -> RepairCase | None:
    from pipeline.store import case_uuid
    for case in load_cases():
        if case_uuid(case) == case_id:
            return case
    return None
