"""Осциллограммы -> DiagnosticRule напрямую (без дистилляции — уже структурно).

Осциллограмма датчика = два правила костяка:
  normal_baseline — «эталонный паттерн» (как должно быть),
  fault           — «сигнатура отказа» (как выглядит неисправность).
Поэтому осциллограммы вливаются прямо в рулбейз, минуя видео-конвейер.

Источники (WebSearch 2026-07-11), по убыванию пригодности «бесплатно+структурно»:
  1. Open Labor Project (openlaborproject.com/waveforms) — БЕСПЛАТНО, 104 машины:
     тип датчика, диапазон напряжений, эталонный паттерн, сигнатура отказа,
     советы. Идеально ложится в normal_baseline+fault. ← начать отсюда.
  2. PicoScope Waveform Library (picoauto.com) — тысячи community-осциллограмм,
     поиск по марке/модели/коду двигателя; «good» и «bad».
  3. AUTOSCOPE (usbautoscope.eu/library), autodiagnosticsandpublishing (~600).
  4. iATN (92k) — премиум, не берём.

Контент-политика (README §5.1): забираем ФАКТЫ (диапазоны, «эталон/отказ»),
не копируем сами картинки-осциллограммы; ссылку на источник сохраняем.

Статус: скелет-парсер. Open Labor Project отдаёт таблицы — селекторы уточнить
на живой странице (HAR не собран). Каждая строка -> два DiagnosticRule.
"""
from __future__ import annotations

from .case_schema import DiagnosticRule


def waveform_to_rules(entry: dict) -> list[DiagnosticRule]:
    """entry: {make, model, sensor, unit, normal, failure, source_url}.
    -> [эталон normal_baseline, сигнатура fault]."""
    rules = []
    if entry.get("normal"):
        rules.append(DiagnosticRule(
            parameter=f"{entry['sensor']} (осциллограмма)",
            condition=entry.get("range", ""), unit=entry.get("unit", ""),
            conclusion=f"эталонный паттерн: {entry['normal']}",
            kind="normal_baseline",
            scope="model" if entry.get("model") else "make",
            make=entry.get("make", ""), model=entry.get("model", ""),
            confidence=0.7,
            caveat="смотри форму сигнала, а не только амплитуду; мерь на рабочем режиме"))
    if entry.get("failure"):
        rules.append(DiagnosticRule(
            parameter=f"{entry['sensor']} (осциллограмма)",
            conclusion=f"сигнатура отказа: {entry['failure']}",
            kind="fault",
            scope="model" if entry.get("model") else "make",
            make=entry.get("make", ""), model=entry.get("model", ""),
            confidence=0.7))
    return rules


# TODO: parse_openlaborproject() — собрать HAR со страницы waveforms, добавить
# селекторы таблицы, эмитить waveform_to_rules() -> писать в data/cases.jsonl
# как «кейс-осциллограмма» (Source.type="waveform") либо прямо в рулбейз.
