"""AutoMech API — бекэнд для мобилки и веба.

    uvicorn backend.app:app --host 0.0.0.0 --port 8000

Эндпоинты:
    GET  /health
    GET  /search?q=...&make=&model=&system=&dtc=&lang=&limit=10
    GET  /case/{id}
    POST /answer   {"q": "...", "filters": {...}, "limit": 5}
    GET  /         мини-веб для проверки руками

Авторизация MVP: если задан BACKEND_API_KEY — требуем заголовок X-Api-Key.
Целевой хостинг: Oracle Free Tier ARM VM (см. README фаза 5).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.answer import compose_answer, compose_consult  # noqa: E402
from backend.search import get_case, load_cases, search  # noqa: E402

API_KEY = os.getenv("BACKEND_API_KEY", "")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="AutoMech API", version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


def _auth(x_api_key: str) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "bad api key")


@app.get("/health")
def health():
    return {"ok": True, "cases_local": len(load_cases())}


@app.get("/search")
def search_endpoint(q: str = Query(min_length=2), make: str = "", model: str = "",
                    system: str = "", dtc: str = "", lang: str = "",
                    limit: int = 10, x_api_key: str = Header(default="")):
    _auth(x_api_key)
    flt = {"make": make, "model": model, "system": system,
           "dtc": dtc, "lang": lang}
    return {"query": q, "results": search(q, flt, min(limit, 50))}


@app.get("/case/{case_id}")
def case_endpoint(case_id: str, x_api_key: str = Header(default="")):
    _auth(x_api_key)
    case = get_case(case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    return case.model_dump()


class AnswerReq(BaseModel):
    q: str
    filters: dict = {}
    limit: int = 5


@app.post("/answer")
def answer_endpoint(req: AnswerReq, x_api_key: str = Header(default="")):
    _auth(x_api_key)
    hits = search(req.q, req.filters, min(req.limit, 10))
    result = compose_answer(req.q, hits)
    result["results"] = hits
    return result


class Observation(BaseModel):
    parameter: str
    value: float | None = None
    unit: str = ""


class ConsultReq(BaseModel):
    make: str = ""
    model: str = ""
    engine: str = ""
    question: str
    observations: list[Observation] = []
    dtc: list[str] = []
    limit: int = 6


@app.post("/consult")
def consult_endpoint(req: ConsultReq, x_api_key: str = Header(default="")):
    """Консультация с костяком правил (причина не теряется) + скепсисом."""
    _auth(x_api_key)
    from pipeline import rulebase
    vehicle = {"make": req.make, "model": req.model, "engine": req.engine}
    # поиск похожих кейсов по жалобе + фильтр по машине (с учётом применимости)
    flt = {"make": req.make, "model": req.model}
    hits = search(req.question, flt, min(req.limit, 10))

    # активируем правила: по каждому замеру + по общей жалобе
    merged = {"direct": [], "hints": [], "general": [], "caveats": []}
    seen = set()
    queries = [(o.parameter, o.value, o.unit) for o in req.observations]
    queries += [(d, None, "") for d in req.dtc]
    queries.append((req.question, None, ""))
    for param, value, unit in queries:
        r = rulebase.match(make=req.make, parameter=param, value=value,
                           unit=unit, query_text=req.question)
        for bucket in ("direct", "hints", "general"):
            for item in r[bucket]:
                key = (item["parameter"], item["conclusion"])
                if key not in seen:
                    seen.add(key)
                    merged[bucket].append(item)
        merged["caveats"] = r["caveats"]  # одинаковы, перезапись ок

    result = compose_consult(vehicle, req.question, hits, merged)
    result["results"] = hits
    return result


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
