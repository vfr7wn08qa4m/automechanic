"""Транскрипт видео БЕЗ домашнего раннера: цепочка сторонних источников.

Порядок (env SUBTITLE_PROVIDERS, по умолчанию "ytdlp,invidious,supadata"):

1. ytdlp      — напрямую с YouTube. С чистого IP работает, с датацентрового
                почти сразу 429. Поддерживает прокси (YTDLP_PROXY —
                residential-прокси решает проблему CI за копейки).
2. invidious  — публичные Invidious-инстансы (бесплатные зеркала YouTube,
                /api/v1/captions). Инстансы смертны: список в env
                INVIDIOUS_INSTANCES, актуальные — https://api.invidious.io.
3. supadata   — коммерческий transcript-API с бесплатным тиром
                (https://supadata.ai, SUPADATA_API_KEY). Стабильно, но квота.

Каждый провайдер сам пропускает себя, если не сконфигурирован/недоступен.
Результат единый: TranscriptResult(lang, lines=[(sec, text)], raw, raw_ext).
"""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field

import requests

from . import config
from .subtitles import fetch_subtitles, vtt_to_lines


@dataclass
class TranscriptResult:
    lang: str
    lines: list[tuple[int, str]]
    raw: str = ""                    # исходный артефакт для архива
    raw_ext: str = "vtt"             # vtt | json
    provider: str = ""
    errors: list[str] = field(default_factory=list)


SUBTITLE_PROVIDERS = [p.strip() for p in os.getenv(
    "SUBTITLE_PROVIDERS", "ytdlp,invidious,supadata").split(",") if p.strip()]

INVIDIOUS_INSTANCES = [i.strip() for i in os.getenv(
    "INVIDIOUS_INSTANCES",
    "https://inv.nadeko.net,https://yewtu.be,https://invidious.nerdvpn.de,"
    "https://iv.melmac.space,https://invidious.f5.si").split(",") if i.strip()]

SUPADATA_API_KEY = os.getenv("SUPADATA_API_KEY", "")


def _langs_priority() -> list[str]:
    return [l.strip() for l in config.SUB_LANGS if l.strip()]


# --- 1. yt-dlp ------------------------------------------------------------------

def _via_ytdlp(video_id: str) -> TranscriptResult:
    url = f"https://www.youtube.com/watch?v={video_id}"
    lang, vtt = fetch_subtitles(url)  # уважает YTDLP_PROXY через env yt-dlp
    return TranscriptResult(lang=lang, lines=vtt_to_lines(vtt), raw=vtt,
                            raw_ext="vtt", provider="ytdlp")


# --- 2. Invidious ----------------------------------------------------------------

def _via_invidious(video_id: str) -> TranscriptResult:
    instances = INVIDIOUS_INSTANCES[:]
    random.shuffle(instances)  # размазываем нагрузку по зеркалам
    last_err = "no instances"
    for inst in instances:
        try:
            r = requests.get(f"{inst}/api/v1/captions/{video_id}", timeout=20)
            if r.status_code != 200:
                last_err = f"{inst}: HTTP {r.status_code}"
                continue
            captions = r.json().get("captions", [])
            if not captions:
                last_err = f"{inst}: no captions"
                continue
            # выбираем дорожку: ручная в приоритетном языке > авто > первая
            def cap_lang(c: dict) -> str:
                return (c.get("languageCode") or c.get("language_code") or "")[:2]

            def rank(c: dict) -> tuple:
                is_auto = "auto" in (c.get("label") or "").lower()
                try:
                    lang_rank = _langs_priority().index(cap_lang(c))
                except ValueError:
                    lang_rank = 99
                return (is_auto, lang_rank)
            best = sorted(captions, key=rank)[0]
            vtt = ""
            # многие инстансы листят титры, но отдают пустой body (YouTube режет
            # им timedtext) — пробуем оба варианта запроса, потом идём дальше
            for cap_url in (f"{inst}{best['url']}",
                            f"{inst}/api/v1/captions/{video_id}?lang={cap_lang(best)}"):
                vtt_r = requests.get(cap_url, timeout=30)
                if vtt_r.status_code == 200 and vtt_r.text.strip():
                    vtt = vtt_r.text
                    break
            if not vtt:
                last_err = f"{inst}: empty caption body"
                continue
            return TranscriptResult(
                lang=cap_lang(best),
                lines=vtt_to_lines(vtt), raw=vtt, raw_ext="vtt",
                provider=f"invidious:{inst}")
        except Exception as e:  # noqa: BLE001 — инстанс лёг, пробуем следующий
            last_err = f"{inst}: {e}"
    raise RuntimeError(f"invidious failed: {last_err}")


# --- 3. Supadata -----------------------------------------------------------------

def _via_supadata(video_id: str) -> TranscriptResult:
    if not SUPADATA_API_KEY:
        raise RuntimeError("SUPADATA_API_KEY не задан")
    r = requests.get(
        "https://api.supadata.ai/v1/youtube/transcript",
        params={"videoId": video_id},
        headers={"x-api-key": SUPADATA_API_KEY}, timeout=60)
    r.raise_for_status()
    body = r.json()
    content = body.get("content") or []
    if not content:
        raise RuntimeError("supadata: empty transcript")
    lines = [(int(c.get("offset", 0) / 1000), (c.get("text") or "").strip())
             for c in content if (c.get("text") or "").strip()]
    return TranscriptResult(
        lang=(body.get("lang") or "")[:2], lines=lines,
        raw=json.dumps(body, ensure_ascii=False), raw_ext="json",
        provider="supadata")


_PROVIDERS = {"ytdlp": _via_ytdlp, "invidious": _via_invidious,
              "supadata": _via_supadata}


def lines_from_raw(raw_ext: str, raw: str) -> list[tuple[int, str]]:
    """Восстановить [(sec|idx, text)] из архивного артефакта:
    vtt | supadata-json (content+offset) | форумный json (posts)."""
    if raw_ext == "json":
        body = json.loads(raw)
        if "posts" in body:  # форумный тред: индекс поста вместо секунды
            return [(i, f"{(p.get('author') or 'user')}: {(p.get('text') or '').strip()}")
                    for i, p in enumerate(body["posts"], 1)
                    if (p.get("text") or "").strip()]
        return [(int(c.get("offset", 0) / 1000), (c.get("text") or "").strip())
                for c in body.get("content", []) if (c.get("text") or "").strip()]
    return vtt_to_lines(raw)


def transcript_for_item(url: str, video_id: str) -> TranscriptResult:
    """Роутер по источнику: CarCareKiosk -> ASR по mp4; иначе YouTube-цепочка.
    url — реальный адрес источника из description work item'а."""
    if "carcarekiosk.com" in (url or ""):
        import json as _json
        from .carcarekiosk import transcript_for
        lang, lines = transcript_for(url)
        raw = _json.dumps(  # supadata-совместимая форма — читается lines_from_raw
            {"lang": lang,
             "content": [{"offset": sec * 1000, "text": text}
                         for sec, text in lines]}, ensure_ascii=False)
        return TranscriptResult(lang=lang, lines=lines, raw=raw,
                                raw_ext="json", provider="carcarekiosk-asr")
    return get_transcript(video_id)


def get_transcript(video_id: str) -> TranscriptResult:
    """Пройти по цепочке провайдеров, вернуть первый успешный транскрипт."""
    errors: list[str] = []
    for name in SUBTITLE_PROVIDERS:
        fn = _PROVIDERS.get(name)
        if fn is None:
            errors.append(f"{name}: unknown provider")
            continue
        try:
            res = fn(video_id)
            if res.lines:
                res.errors = errors
                return res
            errors.append(f"{name}: empty")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {str(e)[:160]}")
    raise RuntimeError(f"транскрипт не добыт ({video_id}): " + " | ".join(errors))
