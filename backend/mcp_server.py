"""MCP-сервер AutoMech — база ремонт-кейсов + костяк правил как ИНСТРУМЕНТЫ для
AI-ассистента мастера (Claude Desktop / Claude Code / любой MCP-клиент).

Идея: шарящий мастер (диагност, спец по прошивке/ремонту ЭБУ) подключает этот
сервер к своему Claude и спрашивает нашу базу прямо в работе — «BMW X3M не
крутит вентилятор», «правило по давлению в рампе на дизеле», «дай кейсы по
CAS3+ увалу». Возвращаем СТРУКТУРНЫЕ улики (кейсы + активированные правила
«если→то» + доктрину скепсиса), а прозу-вывод собирает уже Claude клиента —
так умнее и без зависимости от слабой LLM.

Обёртка над тем же слоем, что и веб-бэкенд: backend.search / pipeline.rulebase.

Запуск (stdio):  python -m backend.mcp_server
Env: QDRANT_URL, QDRANT_API_KEY, CF_ACCOUNT_ID, CF_API_TOKEN (эмбеддинги bge-m3).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP          # noqa: E402
from backend.search import search as _search    # noqa: E402
from pipeline import rulebase                    # noqa: E402

mcp = FastMCP("automech-repair")


def _case(h: dict) -> dict:
    """Компактный кейс из hit'а Qdrant для выдачи клиенту."""
    p = h.get("payload", {})
    return {
        "score": h.get("score"),
        "vehicle": f"{p.get('make','')} {p.get('model','')}".strip(),
        "system": p.get("system", ""),
        "symptoms": p.get("symptoms", []),
        "problem": p.get("problem_summary", ""),
        "root_cause": p.get("root_cause", ""),
        "applicability": p.get("applicability", ""),
        "applicability_note": p.get("applicability_note", ""),
        "fixed": p.get("fixed"),
        "title": p.get("title", ""),
        "url": p.get("url", ""),
    }


@mcp.tool()
def search_repair_cases(query: str, make: str = "", model: str = "",
                        limit: int = 8) -> list[dict]:
    """Семантический поиск по базе РЕАЛЬНЫХ ремонт-кейсов автоэлектрики и ремонта
    ЭБУ/прошивок (из видео и профильных форумов). query — жалоба/симптом/вопрос
    человеческими словами (напр. «не включается вентилятор охлаждения, LIN» или
    «увалил CAS3+ при прошивке ключа»). make/model — опциональный фильтр по авто;
    универсальные и кросс-марочные знания фильтром не отсекаются. Возвращает список
    кейсов: авто, система, проблема, первопричина, применимость, ссылка на источник."""
    flt = {k: v for k, v in (("make", make), ("model", model)) if v}
    hits = _search(query, flt, max(1, min(limit, 20)))
    return [_case(h) for h in hits]


@mcp.tool()
def consult(make: str, model: str = "", engine: str = "", question: str = "",
            dtc: list[str] | None = None,
            observations: list[dict] | None = None) -> dict:
    """Консультация по неисправности: находит похожие кейсы И активирует «костяк
    правил» (если→то) с учётом марки, плюс доктрину скепсиса. observations — замеры
    вида [{"parameter": "давление в рампе", "value": 180, "unit": "бар"}]; dtc —
    коды ошибок. Возвращает СТРУКТУРУ (cases + rules{direct/hints/general/caveats}
    + guidance) — вывод-подозрение собери сам из неё. ПРАВИЛА важнее пересказа
    кейсов: direct — та же марка; hints — на другой марке было так (уточни);
    general — универсальные/по типу двигателя (применяй смело); caveats — скепсис."""
    flt = {"make": make, "model": model}
    hits = _search(question, flt, 8)
    merged: dict = {"direct": [], "hints": [], "general": [], "caveats": []}
    seen: set = set()
    queries = [(o.get("parameter", ""), o.get("value"), o.get("unit", ""))
               for o in (observations or [])]
    queries += [(d, None, "") for d in (dtc or [])]
    queries.append((question, None, ""))
    for param, value, unit in queries:
        r = rulebase.match(make=make, parameter=param, value=value,
                           unit=unit, query_text=question)
        for bucket in ("direct", "hints", "general"):
            for item in r[bucket]:
                key = (item.get("parameter"), item.get("conclusion"))
                if key not in seen:
                    seen.add(key)
                    merged[bucket].append(item)
        merged["caveats"] = r["caveats"]
    return {
        "vehicle": f"{make} {model} {engine}".strip(),
        "question": question,
        "cases": [_case(h) for h in hits],
        "rules": merged,
        "guidance": ("Собери подозрение из cases+rules. Правила применяй по области: "
                     "general (universal/engine_type) — смело даже для другой марки; "
                     "hints (другая марка) — с оговоркой «на похожей было так»; direct — "
                     "напрямую. ВСЕГДА держи в голове caveats (скепсис: датчик≠истина, "
                     "новая деталь≠рабочая, мерить под нагрузкой). Не выдумывай значений."),
    }


@mcp.tool()
def find_rules(make: str, parameter: str, value: float | None = None,
               unit: str = "") -> dict:
    """Правила «если→то» по конкретному параметру для марки: direct (та же марка),
    hints (кросс-марка — подсказки), general (тип двигателя/универсальные), caveats
    (скепсис). Если задан value+unit — по правилам с числовым порогом даётся вердикт
    норма/неисправность. Пример: make=BMW parameter=«давление в рампе» value=180
    unit=бар — вернёт, норма это или слабый ТНВД, с оговорками."""
    return rulebase.match(make=make, parameter=parameter, value=value,
                          unit=unit, query_text=parameter)


if __name__ == "__main__":
    mcp.run()
