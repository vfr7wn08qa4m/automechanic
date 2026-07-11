"""Схема «кейса ремонта» — главный продуктовый артефакт.

Один кейс = одна проблема из видео/треда форума: что случилось, как искали,
что намерили, в чём была причина, как починили, какие грабли попались.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Vehicle(BaseModel):
    make: str = ""                    # марка: Volkswagen
    model: str = ""                   # модель: Passat B6
    years: str = ""                   # годы/поколение: "2005-2010"
    engine: str = ""                  # двигатель/объём: "1.9 TDI BXE"
    extra: str = ""                   # прочее: коробка, комплектация


class Measurement(BaseModel):
    what: str                         # что мерили: "напряжение на клемме 30 реле"
    where: str = ""                   # где физически: разъём, пин, блок
    expected: str = ""                # норма: "12.6 V"
    actual: str = ""                  # фактически: "9.4 V"
    tool: str = ""                    # чем: мультиметр, осциллограф, сканер
    timestamp_sec: int | None = None  # момент в видео


class Step(BaseModel):
    order: int
    action: str                       # что сделали
    detail: str = ""                  # подробности/значения
    timestamp_sec: int | None = None


# Область применимости знания: от узкой к широкой.
#   model       - только эта модель/поколение
#   make        - вся марка ("на всех VW датчик педали газа даёт максимум ~75%")
#   engine_type - тип силовой установки ("завоздушен ТНВД -> стравить: любой дизель")
#   universal   - любой автомобиль
APPLICABILITY = ("model", "make", "engine_type", "universal")


class Pitfall(BaseModel):
    """Технический нюанс/грабли, всплывшие по ходу — самое ценное."""
    text: str
    scope: str = "model"              # см. APPLICABILITY
    timestamp_sec: int | None = None


class Sound(BaseModel):
    """Звуковой симптом — мастера часто диагностируют ушами."""
    description: str                  # как звучит: шорох, гул, щелчки, треск, свист
    when: str = ""                    # условия: на ходу, при включении зажигания,
                                      # на холодную, под нагрузкой
    depends_on: str = ""              # что меняет звук: обороты / скорость /
                                      # передача / температура / поворот руля
    suspected_source: str = ""        # на что указывает: подшипник КПП, ролик, реле
    timestamp_sec: int | None = None


class DiagnosticRule(BaseModel):
    """Переиспользуемое «если → то» — узел мультифлоу знаний. Именно правила
    не дают причине потеряться: их консультант подставляет явно, а не надеется,
    что модель вспомнит. Пример: VW, датчик педали ≤85% — это норма (фишка).

    kind:
      normal_baseline — «это норм / фишка марки» (не считать неисправностью)
      fault           — признак неисправности («<250 бар в рампе → слабый ТНВД»)
      procedure       — действие-правило («завоздушен ТНВД → стравить воздух»)
      caveat          — принцип скепсиса («новая деталь ≠ рабочая»)
    """
    parameter: str                    # что наблюдаем: "датчик педали газа, %",
                                      # "давление в рампе", "звук", DTC "P0087"
    condition: str = ""               # человекочитаемо: "максимум ~85%", "<250 бар"
    op: str = ""                      # структурно (если численно): <= < >= > ~ = !=
    value: float | None = None
    unit: str = ""
    conclusion: str = ""              # что значит: "норма, фишка VW", "слабый ТНВД"
    kind: str = "fault"
    scope: str = "model"              # APPLICABILITY
    make: str = ""                    # к какой марке привязано (для кросс-марки)
    model: str = ""
    engine: str = ""
    confidence: float = 0.5
    caveat: str = ""                  # скепсис: "мерить под нагрузкой; показание
                                      # само по себе не доказывает исправность"
    timestamp_sec: int | None = None


class Source(BaseModel):
    type: str = "youtube"             # youtube | forum
    url: str = ""
    video_id: str = ""
    channel: str = ""
    channel_id: str = ""
    title: str = ""
    lang: str = ""
    published_at: str = ""
    duration_sec: int | None = None


class RepairCase(BaseModel):
    source: Source = Field(default_factory=Source)
    vehicle: Vehicle = Field(default_factory=Vehicle)

    system: str = ""                  # подсистема: стартер, генератор, CAN, ЭБУ,
                                      # иммобилайзер, освещение, ЦЗ, датчики...
    symptoms: list[str] = []          # жалобы/симптомы, как их описал бы клиент
    sounds: list[Sound] = []          # звуковые симптомы (отдельно — по ним ищут)
    dtc_codes: list[str] = []         # коды ошибок: P0301, U0100, B1342...
    problem_summary: str = ""         # суть проблемы, 1-3 предложения
    root_cause: str = ""              # найденная первопричина
    fixed: bool | None = None         # починили ли в этом видео
    applicability: str = "model"      # насколько широко применим кейс (APPLICABILITY)
    applicability_note: str = ""      # словами: "любой дизель с ТНВД", "все VW группы"

    diagnostic_steps: list[Step] = []
    repair_steps: list[Step] = []
    measurements: list[Measurement] = []
    parts: list[str] = []             # детали (с OEM-номерами, если звучали)
    tools: list[str] = []
    pitfalls: list[Pitfall] = []      # нюансы: «не перепутай, там два одинаковых разъёма»
    rules: list[DiagnosticRule] = []  # костяк «если→то» из этого видео (см. DiagnosticRule)

    lang: str = ""                    # язык исходника
    summary_en: str = ""              # каноническое резюме на английском (для кросс-языка)
    off_topic: bool = False           # видео не про ремонт автоэлектрики
    confidence: float = 0.0           # 0..1 — уверенность модели в извлечении
    distill_model: str = ""           # какой моделью извлечено (для пере-дистилляции)

    def search_text(self) -> str:
        """Текст, который кодируем в вектор (оригинальный язык + EN-резюме)."""
        sounds = "; ".join(
            f"{s.description} {s.when} {s.depends_on} {s.suspected_source}".strip()
            for s in self.sounds)
        parts = [
            f"{self.vehicle.make} {self.vehicle.model} {self.vehicle.years} {self.vehicle.engine}".strip(),
            self.system,
            "; ".join(self.symptoms),
            sounds,
            " ".join(self.dtc_codes),
            self.problem_summary,
            self.root_cause,
            self.applicability_note,
            "; ".join(p.text for p in self.pitfalls),
            "; ".join(f"{r.parameter} {r.condition} → {r.conclusion}" for r in self.rules),
            self.summary_en,
        ]
        return "\n".join(p for p in parts if p)

    def rules_with_context(self) -> list[DiagnosticRule]:
        """Правила с проставленной маркой/моделью из кузова кейса (для рулбейза)."""
        out = []
        for r in self.rules:
            rr = r.model_copy()
            rr.make = rr.make or self.vehicle.make
            rr.model = rr.model or self.vehicle.model
            rr.engine = rr.engine or self.vehicle.engine
            out.append(rr)
        return out


# JSON-скелет для промпта (короче и надёжнее, чем полная JSON Schema)
CASE_JSON_TEMPLATE = """{
  "vehicle": {"make": "", "model": "", "years": "", "engine": "", "extra": ""},
  "system": "",
  "symptoms": [""],
  "sounds": [{"description": "", "when": "", "depends_on": "", "suspected_source": "", "timestamp_sec": 0}],
  "dtc_codes": [""],
  "problem_summary": "",
  "root_cause": "",
  "fixed": true,
  "applicability": "model|make|engine_type|universal",
  "applicability_note": "",
  "diagnostic_steps": [{"order": 1, "action": "", "detail": "", "timestamp_sec": 0}],
  "repair_steps": [{"order": 1, "action": "", "detail": "", "timestamp_sec": 0}],
  "measurements": [{"what": "", "where": "", "expected": "", "actual": "", "tool": "", "timestamp_sec": 0}],
  "parts": [""],
  "tools": [""],
  "pitfalls": [{"text": "", "scope": "model|make|engine_type|universal", "timestamp_sec": 0}],
  "rules": [{"parameter": "", "condition": "", "op": "<=|<|>=|>|~|=|!=", "value": 0, "unit": "", "conclusion": "", "kind": "normal_baseline|fault|procedure|caveat", "scope": "model|make|engine_type|universal", "confidence": 0.0, "caveat": "", "timestamp_sec": 0}],
  "lang": "",
  "summary_en": "",
  "off_topic": false,
  "confidence": 0.0
}"""
