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

# страховка от обрезанных транскриптов: меньше этого числа строк = провайдер
# недотянул (интро/огрызок) -> роутер пробует следующий провайдер.
_MIN_TRANSCRIPT_LINES = int(os.getenv("TRANSCRIPT_MIN_LINES", "3"))


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


# --- 4. tubetranscript (через Playwright) ---------------------------------------
# Сторонний сервис сам тянет YouTube на СВОЁЙ стороне -> облачный DC-IP не мешает.
# Без токена (nginx, не Cloudflare); JS рендерит транскрипт -> ведём браузером.
_TT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_TT_PW: dict = {"pw": None, "browser": None, "ctx": None}


def _tt_ctx():
    if _TT_PW["ctx"] is None:
        from playwright.sync_api import sync_playwright
        _TT_PW["pw"] = sync_playwright().start()
        _TT_PW["browser"] = _TT_PW["pw"].chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        _TT_PW["ctx"] = _TT_PW["browser"].new_context(user_agent=_TT_UA, locale="en-US")
    return _TT_PW["ctx"]


def close_tubetranscript() -> None:
    if _TT_PW["ctx"] is not None:
        try:
            _TT_PW["browser"].close()
            _TT_PW["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
        _TT_PW.update(pw=None, browser=None, ctx=None)


def _via_tubetranscript(video_id: str) -> TranscriptResult:
    # Фронт tubetranscript дёргает свой transcript-API (yt-to-text.com/api/v1/Subtitles)
    # -> ПЕРЕХВАТЫВАЕМ его ответ = ПОЛНЫЙ транскрипт с таймкодами, минуя DOM. Старый
    # путь читал .transcript-text из ВИРТУАЛИЗОВАННОГО списка -> в DOM только ~2
    # видимых сегмента -> обрезка «2 строки» (баг, из-за которого падала дистилляция).
    page = _tt_ctx().new_page()
    captured: dict = {}

    def _on_resp(resp):
        if "yt-to-text.com/api/v1/Subtitles" in resp.url and resp.status == 200:
            try:
                captured["data"] = resp.json()
            except Exception:  # noqa: BLE001
                pass

    page.on("response", _on_resp)
    try:
        page.goto(f"https://tubetranscript.com/en/watch?v={video_id}",
                  wait_until="domcontentloaded", timeout=45000)
        trs = None
        for _ in range(40):                    # ждём, пока фронт получит транскрипт
            d = captured.get("data")
            trs = ((d or {}).get("data") or {}).get("transcripts")
            if trs:
                break
            page.wait_for_timeout(1000)
        if not trs:
            raise RuntimeError("tubetranscript: API yt-to-text не отдал transcripts")
        lines = [(int(float(t.get("s") or 0)), (t.get("t") or "").strip())
                 for t in trs if (t.get("t") or "").strip()]
        if not lines:
            raise RuntimeError("tubetranscript: пустой транскрипт (API)")
        raw = json.dumps(
            {"content": [{"offset": int(float(t.get("s") or 0) * 1000),
                          "text": t.get("t") or ""} for t in trs]},
            ensure_ascii=False)
        return TranscriptResult(lang="", lines=lines, raw=raw, raw_ext="json",
                                provider="tubetranscript")
    finally:
        page.close()


# --- 5. youtube-transcript.io (через Playwright, перехват JSON-API) --------------
# Чистый JSON: POST /api/transcripts/v2 {"ids":[id]} -> success[0].tracks[0].
# transcript = [{start,dur,text}] с таймкодами. Сервис сам тянет YouTube ->
# облачный DC-IP не мешает. Нужен их анти-бот заголовок x-is-human (генерит их
# же JS) -> ведём страницу тем же headless-браузером и перехватываем ответ API.
def _via_yttio(video_id: str) -> TranscriptResult:
    page = _tt_ctx().new_page()
    captured: dict = {}

    def _on_resp(resp):
        if "/api/transcripts/v2" in resp.url and resp.status == 200:
            try:
                captured["data"] = resp.json()
            except Exception:  # noqa: BLE001
                pass
    page.on("response", _on_resp)
    try:
        page.goto(f"https://www.youtube-transcript.io/videos?id={video_id}",
                  wait_until="domcontentloaded", timeout=45000)
        for _ in range(40):                 # ждём, пока их JS дёрнет api (x-is-human)
            if captured.get("data"):
                break
            page.wait_for_timeout(1000)
        data = captured.get("data")
        if not data:
            raise RuntimeError("yttio: ответ api/transcripts не перехвачен")
        succ = data.get("success") or []
        if not succ:
            raise RuntimeError("yttio: success пуст (видео недоступно/без титров)")
        item = succ[0]
        tracks = item.get("tracks") or []
        segs = (tracks[0].get("transcript") if tracks else None) or []
        lines: list[tuple[int, str]] = []
        for s in segs:
            t = (s.get("text") or "").strip()
            if not t:
                continue                    # часто \n-заглушки между строками
            try:
                sec = int(float(s.get("start", 0)))
            except (TypeError, ValueError):
                sec = 0
            lines.append((sec, t))
        if not lines and (item.get("text") or "").strip():
            lines = [(0, item["text"].strip())]     # фолбэк: сплошной текст
        if not lines:
            raise RuntimeError("yttio: пустой транскрипт")
        lang = ((tracks[0].get("language") if tracks else "") or "")[:2]
        raw = json.dumps({"lang": lang,
                          "content": [{"offset": sec * 1000, "text": t}
                                      for sec, t in lines]}, ensure_ascii=False)
        return TranscriptResult(lang=lang, lines=lines, raw=raw,
                                raw_ext="json", provider="yttio")
    finally:
        page.close()


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
              "supadata": _via_supadata, "tubetranscript": _via_tubetranscript,
              "yttio": _via_yttio}


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
    print(f"[subs] видео {video_id}: цепочка провайдеров = {SUBTITLE_PROVIDERS}")
    errors: list[str] = []
    for name in SUBTITLE_PROVIDERS:
        print(f"[subs]   пробую {name}...")
        fn = _PROVIDERS.get(name)
        if fn is None:
            errors.append(f"{name}: unknown provider")
            print(f"[subs]     ✗ {name} not found in _PROVIDERS")
            continue
        try:
            res = fn(video_id)
            print(f"[subs]     результат: {len(res.lines)} строк, provider={res.provider}")
            # страховка от обрезки: реальный кейс-транскрипт — это десятки+ строк.
            # 1-2 строки = провайдер недотянул (напр. отдал интро) -> следующий.
            if res.lines and len(res.lines) >= _MIN_TRANSCRIPT_LINES:
                print(f"[subs]   ✅ успешно через {name}!")
                res.errors = errors
                return res
            errors.append(f"{name}: короткий/пустой ({len(res.lines)} строк)")
            print(f"[subs]     ⚠ слишком короткий, пробую следующий")
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)[:160]
            errors.append(f"{name}: {err_msg}")
            print(f"[subs]     ✗ ошибка: {err_msg}")
    raise RuntimeError(f"транскрипт не добыт ({video_id}): " + " | ".join(errors))
