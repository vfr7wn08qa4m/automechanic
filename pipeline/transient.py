"""Отличить ВРЕМЕННУЮ ошибку провайдера от настоящей поломки материала.

Зачем: этапы конвейера (эмбеддинг/дистилляция/ASR) ловят Exception и хоронят
work item в Removed+failed. Но 429 (кончился бесплатный тир Cloudflare), 5xx и
таймаут — это НЕ «кейс плохой», это «сейчас занято». Так уже один раз погибли
1178 готовых кейсов (эмбеддинг без провайдера), и так же 429 съедает их снова.

Правило: временная ошибка -> НЕ трогаем состояние (claim ничего не блокирует,
это просто запись в History), айтем останется в очереди и будет взят следующим
тиком. Постоянная -> как раньше, failed.
"""
from __future__ import annotations

import re

# коды, которые значат «повтори позже», а не «материал негодный»
_TRANSIENT_CODES = (408, 409, 425, 429, 500, 502, 503, 504, 529)
_TRANSIENT_WORDS = (
    "too many requests", "rate limit", "rate_limit", "quota",
    "timeout", "timed out", "temporarily", "temporary failure",
    "connection reset", "connection aborted", "connection error",
    "remote end closed", "bad gateway", "service unavailable",
    "gateway timeout", "overloaded", "capacity",
    # «ни один провайдер не ответил» — это всегда про инфраструктуру, а не про
    # материал тикета: каскад дистилляции, цепочка эмбеддингов, цепочка ASR.
    # Сюда же попадает 400 вида «invalid temperature» — сломан ПАРАМЕТР запроса,
    # тикет ни при чём, и хоронить его нельзя (так ушли #2533-2535).
    "каскад исчерпан", "все провайдеры",
)


def is_transient(exc: BaseException) -> bool:
    """True — сбой провайдера/сети, айтем НЕ виноват, можно повторить позже."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    resp = getattr(exc, "response", None)
    if status is None and resp is not None:
        status = getattr(resp, "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_CODES:
        return True

    text = str(exc).lower()
    # каскадные сообщения вида «все провайдеры недоступны: cloudflare: 429 ...»
    if re.search(r"\b(429|500|502|503|504|529)\b", text):
        return True
    return any(w in text for w in _TRANSIENT_WORDS)
