"""RAG-ответ для мобилки: найденные кейсы -> бесплатная LLM формулирует ответ.

Это тот самый слой «бесплатных чат-ботов на бекэнде» (идея юзера): каскад
ANSWER_ENDPOINTS в том же формате, что DISTILL_ENDPOINTS. По умолчанию —
Pollinations (без ключа, проверен). Сильные варианты: NIM, гейтвей чатов
на домашнем сервере, Groq — просто добавь эндпоинты в env.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ANSWER_ENDPOINTS = os.getenv(
    "ANSWER_ENDPOINTS", "https://text.pollinations.ai/openai|none|openai")
ANSWER_TIMEOUT = float(os.getenv("ANSWER_TIMEOUT_SECONDS", "90"))

SYSTEM = """Ты — ассистент автоэлектрика. Тебе дают вопрос мастера и найденные
в базе кейсы реальных ремонтов (из видео/форумов). Правила:
- Отвечай на ЯЗЫКЕ ВОПРОСА, кратко и по делу: с чего начать проверку, какие
  замеры, какая вероятная причина.
- Используй ТОЛЬКО факты из кейсов. Ссылайся на кейсы номерами [1], [2].
- Учитывай применимость кейса: universal/engine_type-знание (напр. «завоздушен
  ТНВД — стравить воздух, любой дизель») применяй смело даже для другой марки;
  model-знание для другой машины давай с оговоркой «на похожей модели было так».
- Обязательно упоминай практические нюансы (pitfalls) из кейсов — это золото.
- Если кейсы не про эту проблему — скажи честно, что точного совпадения нет,
  и предложи ближайшее.
- Никаких выдуманных значений напряжений/сопротивлений."""


def _endpoints() -> list[tuple[str, str, str]]:
    out = []
    for entry in ANSWER_ENDPOINTS.split(";"):
        parts = [p.strip() for p in entry.split("|")]
        if len(parts) != 3:
            continue
        base_url, key_env, model = parts
        key = "none" if key_env.lower() == "none" else os.getenv(key_env, "")
        if key:
            out.append((base_url, key, model))
    return out


def _context(hits: list[dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        p = h["payload"]
        appl = p.get("applicability", "model")
        appl_line = (f"Применимость: {appl}"
                     + (f" — {p['applicability_note']}" if p.get("applicability_note") else ""))
        sounds = "; ".join(p.get("sounds", []))
        blocks.append(
            f"[{i}] {p.get('title', '')} ({p.get('url', '')})\n"
            f"Авто: {p.get('make', '')} {p.get('model', '')} | Система: {p.get('system', '')}\n"
            f"{appl_line}\n"
            f"Симптомы: {'; '.join(p.get('symptoms', []))}\n"
            + (f"Звуки: {sounds}\n" if sounds else "")
            + f"DTC: {', '.join(p.get('dtc_codes', [])) or '-'}\n"
            f"Проблема: {p.get('problem_summary', '')}\n"
            f"Причина: {p.get('root_cause', '')}\n"
            f"Починено в источнике: {p.get('fixed')}")
    return "\n\n".join(blocks)


def _load_doctrine() -> str:
    from pathlib import Path
    f = Path(__file__).resolve().parent.parent / "claude" / "CONSULT_DOCTRINE.md"
    return f.read_text(encoding="utf-8") if f.exists() else SYSTEM


def _fmt_rules(rules: dict) -> str:
    lines = []
    for bucket, label in (("direct", "ПРЯМЫЕ правила (та же марка)"),
                          ("hints", "КРОСС-МАРКА (подсказки, уточнить)"),
                          ("general", "ОБЩИЕ (тип двигателя/универсальные)"),
                          ("caveats", "СКЕПСИС (применяй всегда)")):
        items = rules.get(bucket) or []
        if not items:
            continue
        lines.append(f"\n[{label}]")
        for r in items:
            v = f" | вывод по замеру: {r['verdict']}" if r.get("verdict") else ""
            cav = f" | скепсис: {r['caveat']}" if r.get("caveat") else ""
            mk = f" ({r['make']})" if r.get("make") else ""
            lines.append(f"- {r['parameter']}{mk}: {r.get('condition','')} → "
                         f"{r['conclusion']}{v}{cav}")
    return "\n".join(lines)


def compose_consult(vehicle: dict, question: str, hits: list[dict],
                    rules: dict) -> dict:
    """Консультация: кейсы + активированные правила + доктрина скепсиса."""
    doctrine = _load_doctrine()
    veh = (f"{vehicle.get('make','')} {vehicle.get('model','')} "
           f"{vehicle.get('engine','')}".strip() or "марка не указана")
    ctx = _context(hits) if hits else "(похожих кейсов в базе не найдено)"
    user = (f"Машина клиента: {veh}\nЖалоба/вопрос: {question}\n\n"
            f"АКТИВИРОВАННЫЕ ПРАВИЛА (костяк знаний):{_fmt_rules(rules)}\n\n"
            f"КЕЙСЫ из базы:\n{ctx}")
    errors = []
    for base_url, key, model in _endpoints():
        try:
            client = OpenAI(api_key=key, base_url=base_url,
                            timeout=ANSWER_TIMEOUT, max_retries=0)
            resp = client.chat.completions.create(
                model=model, temperature=0.3, max_tokens=1400,
                messages=[{"role": "system", "content": doctrine},
                          {"role": "user", "content": user}])
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return {"answer": text, "model": model,
                        "rules_used": rules, "sources": [
                            {"n": i + 1, "title": h["payload"].get("title", ""),
                             "url": h["payload"].get("url", "")}
                            for i, h in enumerate(hits)]}
        except Exception as e:  # noqa: BLE001
            errors.append(f"{model}: {str(e)[:120]}")
    return {"answer": "", "rules_used": rules, "error": "; ".join(errors)}


def compose_answer(query: str, hits: list[dict]) -> dict:
    if not hits:
        return {"answer": "", "sources": [],
                "note": "по запросу ничего не найдено в базе кейсов"}
    user = f"Вопрос мастера: {query}\n\nКейсы из базы:\n\n{_context(hits)}"
    errors = []
    for base_url, key, model in _endpoints():
        try:
            client = OpenAI(api_key=key, base_url=base_url,
                            timeout=ANSWER_TIMEOUT, max_retries=0)
            resp = client.chat.completions.create(
                model=model, temperature=0.3, max_tokens=1200,
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": user}])
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return {"answer": text, "model": model,
                        "sources": [{"n": i + 1,
                                     "title": h["payload"].get("title", ""),
                                     "url": h["payload"].get("url", "")}
                                    for i, h in enumerate(hits)]}
        except Exception as e:  # noqa: BLE001 — каскадим
            errors.append(f"{model}: {str(e)[:120]}")
    return {"answer": "", "sources": [], "error": "; ".join(errors)}
