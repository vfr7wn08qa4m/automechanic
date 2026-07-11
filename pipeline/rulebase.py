"""Рулбейз — «костяк» знаний: правила если→то со всех кейсов + скепсис-доктрина.

Задача: чтобы ПРИЧИНА НЕ ТЕРЯЛАСЬ. Консультант не надеется, что LLM вспомнит
факт «на VW педаль ≤85% — норма»; рулбейз подставляет подходящие правила ЯВНО
как структурированный контекст, плюс всегда добавляет универсальные caveat'ы
скепсиса (новая деталь ≠ рабочая; VIN мог быть перебит; и т.д.).

Мультифлоу вместо жёсткого дерева: вход {марка, параметр, значение...} активирует
релевантные правила-узлы. Марка не совпала — правило приходит как кросс-марочная
подсказка («на VW так; у тебя другая марка — возможно похоже, уточни»).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config
from .case_schema import DiagnosticRule, RepairCase

DOCTRINE_DIR = Path(__file__).parent / "doctrine"

_OPS = {
    "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
    "=": lambda a, b: abs(a - b) < 1e-9, "!=": lambda a, b: abs(a - b) >= 1e-9,
    "~": lambda a, b: abs(a - b) <= max(0.1 * abs(b), 1.0),  # ~ ≈ в пределах 10%
}


def load_doctrine() -> list[DiagnosticRule]:
    """Универсальные правила-скепсиса (всегда в выдаче консультанта)."""
    rules = []
    for f in sorted(DOCTRINE_DIR.glob("*.json")):
        for d in json.loads(f.read_text(encoding="utf-8")):
            rules.append(DiagnosticRule.model_validate(d))
    return rules


def load_case_rules() -> list[DiagnosticRule]:
    """Все правила из кейсов (data/cases.jsonl), с проставленной маркой."""
    f = config.DATA_DIR / "cases.jsonl"
    out: list[DiagnosticRule] = []
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            case = RepairCase.model_validate_json(line)
        except Exception:  # noqa: BLE001
            continue
        if case.off_topic:
            continue
        out.extend(case.rules_with_context())
    return out


def _scope_relation(rule: DiagnosticRule, make: str) -> str:
    """Как правило относится к машине клиента:
    direct — прямо применимо; hint — кросс-марочная подсказка; general — универсально."""
    if rule.scope in ("universal", "engine_type"):
        return "general"
    rmk = (rule.make or "").strip().lower()
    cmk = (make or "").strip().lower()
    if not rmk or not cmk:
        return "hint"
    if rmk in cmk or cmk in rmk:
        return "direct"
    return "hint"          # правило другой марки — подсказка «возможно похоже»


def _param_match(rule: DiagnosticRule, text: str) -> float:
    """Грубое совпадение параметра правила с текстом запроса (0..1)."""
    rp = rule.parameter.lower()
    t = text.lower()
    if not rp:
        return 0.0
    if rp in t or t in rp:
        return 1.0
    toks = {w for w in rp.replace(",", " ").split() if len(w) > 3}
    if not toks:
        return 0.0
    hit = sum(1 for w in toks if w in t)
    return hit / len(toks)


def _numeric_verdict(rule: DiagnosticRule, value: float) -> str | None:
    """Если правило численное и есть значение клиента — проверить условие."""
    if rule.op not in _OPS or rule.value is None:
        return None
    ok = _OPS[rule.op](value, rule.value)
    rel = f"{value}{rule.unit} {rule.op} {rule.value}{rule.unit}"
    if rule.kind == "normal_baseline":
        return (f"{rel} — попадает в норму → {rule.conclusion}" if ok
                else f"{rel} — ВНЕ нормы ({rule.condition}) → стоит проверить, "
                     f"обычно должно быть {rule.op} {rule.value}{rule.unit}")
    return (f"{rel} — условие выполнено → {rule.conclusion}" if ok
            else f"{rel} — условие не выполнено, правило не срабатывает")


def match(make: str = "", parameter: str = "", value: float | None = None,
          unit: str = "", query_text: str = "", limit: int = 8) -> dict:
    """Активировать релевантные правила. Возвращает:
       {direct: [...], hints: [...], caveats: [...]} — уже отранжированные."""
    text = f"{parameter} {query_text}".strip()
    scored: list[tuple[float, dict]] = []
    for rule in load_case_rules():
        pm = _param_match(rule, text)
        if pm < 0.34:
            continue
        rel = _scope_relation(rule, make)
        verdict = _numeric_verdict(rule, value) if value is not None else None
        score = pm * rule.confidence * (1.0 if rel == "direct" else
                                        0.7 if rel == "general" else 0.5)
        if verdict:
            score += 0.5
        scored.append((score, {
            "parameter": rule.parameter, "condition": rule.condition,
            "conclusion": rule.conclusion, "kind": rule.kind, "scope": rule.scope,
            "make": rule.make, "relation": rel, "confidence": rule.confidence,
            "caveat": rule.caveat, "verdict": verdict,
        }))
    scored.sort(key=lambda x: -x[0])
    top = [r for _s, r in scored[:limit]]
    return {
        "direct": [r for r in top if r["relation"] == "direct"],
        "hints": [r for r in top if r["relation"] == "hint"],
        "general": [r for r in top if r["relation"] == "general"],
        "caveats": [r.model_dump() for r in load_doctrine()],  # всегда
    }
