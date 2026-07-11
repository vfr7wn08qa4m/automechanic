"""Дистилляция: титры -> структурированный кейс ремонта (RepairCase).

Работает с любым OpenAI-совместимым endpoint (NVIDIA NIM, DeepSeek, Kimi,
локальный free-claude-code gateway, Ollama). Настройка в config.py.
"""
from __future__ import annotations

import json
import re
import time

from openai import OpenAI

from . import config
from .case_schema import CASE_JSON_TEMPLATE, RepairCase, Source

SYSTEM_PROMPT = """You are an expert auto electrician and data extractor.
You receive a transcript of a car-repair video (any language) with [mm:ss] timestamps.
Extract ONE structured repair case as JSON exactly matching this template:

__CASE_JSON_TEMPLATE__

Rules:
- Keep text fields in the ORIGINAL transcript language, except summary_en (English).
- symptoms: complaints as a customer would state them (searchable phrases).
- sounds: EVERY noise mentioned (rattle, hum, click, whistle, grind...) as a separate entry:
  how it sounds (description), when it appears (when), what changes it (depends_on:
  RPM / speed / gear / temperature / steering / load), and what it points to
  (suspected_source). Mechanics search by sound — extract these carefully.
- dtc_codes: only real diagnostic trouble codes heard in transcript (P/U/B/C####). Empty list if none.
- measurements: every voltage/resistance/current/pressure actually measured, with expected vs actual when stated.
- pitfalls: every practical gotcha, warning, non-obvious nuance the mechanic mentions in passing. These are the most valuable — do not skip any.
- rules: THE MOST IMPORTANT reusable knowledge — every "if X then Y" fact stated,
  even in passing, that helps diagnose OTHER cars later. Extract each as a rule:
    * parameter — what is observed ("accelerator pedal sensor, %", "rail pressure",
      a sound, a DTC like "P0087").
    * condition / op / value / unit — if numeric, fill structured (op one of <= < >= > ~ = !=);
      e.g. "max ~85%" -> op "<=", value 85, unit "%".
    * conclusion — what it means ("normal by design, not a fault", "weak lift pump").
    * kind — normal_baseline (это норм/фишка марки) | fault | procedure (an action to take)
      | caveat (a skepticism principle).
    * scope — model|make|engine_type|universal (how broadly it applies).
    * caveat — any skepticism the mechanic voices (measure under load; a reading alone
      doesn't prove a part is good; a new part can be defective; VIN/engine may be swapped).
  Example rule: {"parameter":"accelerator pedal sensor, %","op":"<=","value":85,"unit":"%",
  "conclusion":"normal by design on VAG cars","kind":"normal_baseline","scope":"make","confidence":0.8}
- scope (per pitfall) and applicability (whole case): how broadly the knowledge applies —
  "model" (only this model/generation), "make" (whole brand, e.g. "on all VW the pedal
  sensor maxes at ~75%"), "engine_type" (e.g. air in the injection pump -> bleed it: ANY
  diesel), "universal" (any car). Be honest; default to "model" when unsure.
  applicability_note: one phrase in the original language, e.g. "любой дизель с ТНВД".
- timestamp_sec: integer seconds computed from the nearest [mm:ss] marker.
- fixed: true only if the problem is confirmed solved in the video.
- off_topic: true if this is NOT a concrete vehicle electrical/electronic diagnosis or repair (reviews, ads, vlogs, pure mechanics like brakes/suspension without electrics).
- confidence: 0..1, your honest estimate of extraction quality.
- If several unrelated problems are covered, extract the MAIN one, mention others in summary_en.
Return ONLY the JSON object, no markdown fences, no commentary.""".replace(
    "__CASE_JSON_TEMPLATE__", CASE_JSON_TEMPLATE)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _endpoints() -> list[tuple[str, str, str]]:
    """Каскад (base_url, api_key, model). Без DISTILL_ENDPOINTS — один основной."""
    import os
    if config.DISTILL_ENDPOINTS:
        out = []
        for entry in config.DISTILL_ENDPOINTS.split(";"):
            parts = [p.strip() for p in entry.split("|")]
            if len(parts) != 3:
                continue
            base_url, key_env, model = parts
            key = "none" if key_env.lower() == "none" else os.getenv(key_env, "")
            if key:  # эндпоинт без ключа в env молча пропускаем
                out.append((base_url, key, model))
        if out:
            return out
    if not config.DISTILL_API_KEY:
        raise RuntimeError("DISTILL_API_KEY / NIM_API_KEY не задан (см. .env.example)")
    return [(config.DISTILL_BASE_URL, config.DISTILL_API_KEY, config.DISTILL_MODEL)]


def _client(base_url: str, api_key: str) -> OpenAI:
    # без таймаута батч намертво виснет на одном тухлом запросе к провайдеру
    return OpenAI(api_key=api_key, base_url=base_url,
                  timeout=float(config.DISTILL_TIMEOUT_SECONDS), max_retries=1)


def _extract_json(text: str) -> dict:
    # срезаем возможные reasoning-блоки и код-фенсы
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.replace("```json", "```")
    if "```" in text:
        chunks = [c for c in text.split("```") if "{" in c]
        if chunks:
            text = chunks[0]
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:300]}")
    return json.loads(m.group(0))


def distill(transcript: str, source: Source, retries: int = 2) -> RepairCase:
    """Каскад: на каждом эндпоинте до `retries+1` попыток, затем следующий."""
    errors: list[str] = []
    for base_url, api_key, model in _endpoints():
        client = _client(base_url, api_key)
        for attempt in range(retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Video title: {source.title}\nChannel: {source.channel}\n\nTranscript:\n{transcript}"},
                    ],
                    temperature=0.2,
                    max_tokens=config.DISTILL_MAX_TOKENS,
                )
                raw = resp.choices[0].message.content or ""
                data = _extract_json(raw)
                data["source"] = source.model_dump()
                case = RepairCase.model_validate(data)
                case.distill_model = model
                return case
            except Exception as e:  # noqa: BLE001 — ретраим/каскадим любой сбой
                errors.append(f"{model}: {str(e)[:160]}")
                status = getattr(e, "status_code", None)
                if status in (401, 402, 404, 410):
                    break  # ключ/модель мертвы — ретраить бессмысленно, дальше по каскаду
                if attempt < retries:
                    time.sleep(5 * (attempt + 1))
    raise RuntimeError("distill: каскад исчерпан: " + " || ".join(errors))
