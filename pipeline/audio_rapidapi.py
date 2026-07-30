"""Аудио видео через RapidAPI youtube-mp3 (бэкенд robotilab.online).

ЗАЧЕМ: второй независимый путь YouTube→MP3 рядом с convert1s. Оба сервиса тянут
YouTube на СВОИХ серверах, поэтому работают с датацентрового IP (проверено на
GitHub-раннере 2026-07-30: 5/5 реальных кандидатов). Отказы у них ПОШТУЧНЫЕ и
по одним и тем же видео, так что второй провайдер — запас на такие осечки, а не
обход блокировки.

⚠️ ЛОВУШКА API: GET /download/mp3 отвечает мгновенно (0.3с) и отдаёт лишь
СКОНСТРУИРОВАННУЮ ссылку robotilab.online/download-api/yt/audio?url=…
Она возвращается даже для видео, которое скачать нельзя. Судить об успехе по
этому ответу НЕЛЬЗЯ — настоящая тяга с YouTube происходит при скачивании
downloadUrl (22-30с, Content-Type audio/mpeg). Поэтому здесь всегда качаем.

⚠️ ЧЕРЕЗ CRAWL_PROXY (Cloudflare Worker) НЕ ХОДИМ: релей срезает заголовок
x-rapidapi-key, RapidAPI отвечает 401, дальше 429 (замерено в облаке). Только
напрямую — с датацентра это и так работает.

Free tier ~200 запросов/сутки, ключ в env RAPIDAPI_KEY.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def fetch_mp3(video_id: str, workdir: Path | None = None,
              to_16k_mono: bool = True) -> Path:
    """video_id → путь к mp3 (по умолчанию 16кГц моно для Whisper). Кидает при сбое.

    Сигнатура совместима с audio_convert1s.fetch_mp3 — цепочка в
    subtitle_providers._fetch_audio зовёт их одинаково.
    """
    if not config.RAPIDAPI_KEY:
        raise RuntimeError("rapidapi: RAPIDAPI_KEY не задан")
    workdir = Path(workdir or tempfile.mkdtemp(prefix="asr_"))

    q = urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
    api = f"https://{config.RAPIDAPI_HOST}/download/mp3?url={q}"
    req = urllib.request.Request(api, headers={
        "x-rapidapi-host": config.RAPIDAPI_HOST,
        "x-rapidapi-key": config.RAPIDAPI_KEY,
        "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as x:
        resp = json.loads(x.read().decode("utf-8", errors="replace"))
    dl = resp.get("downloadUrl")
    if not dl:
        raise RuntimeError(f"rapidapi: нет downloadUrl ({str(resp)[:200]})")

    # Тут и происходит реальная работа: сервис идёт на YouTube и конвертирует.
    raw = workdir / f"{video_id}.src.mp3"
    dreq = urllib.request.Request(dl, headers={"User-Agent": _UA})
    with urllib.request.urlopen(dreq, timeout=300) as x:
        ctype = (x.headers.get("Content-Type") or "").lower()
        with open(raw, "wb") as f:
            shutil.copyfileobj(x, f)
    if not raw.exists() or raw.stat().st_size < 2000:
        raise RuntimeError(f"rapidapi: пустой/битый файл (ct={ctype})")
    head = raw.open("rb").read(3)
    if "audio" not in ctype and "octet-stream" not in ctype and head != b"ID3":
        raise RuntimeError(f"rapidapi: ответ не аудио (ct={ctype})")

    if not to_16k_mono:
        return raw
    out = workdir / f"{video_id}.mp3"
    try:
        p = subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1",
                            str(out)], capture_output=True, text=True, timeout=180)
        if p.returncode == 0 and out.exists() and out.stat().st_size > 1000:
            raw.unlink(missing_ok=True)
            return out
    except Exception:  # noqa: BLE001 — нет ffmpeg: отдаём исходный mp3
        pass
    return raw
