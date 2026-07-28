"""Аудио видео через сторонний YouTube→MP3 сервис (convert1s, бэкенд ytmp3.gg).

ЗАЧЕМ: yt-dlp с ДАТАЦЕНТРА (ADO/CircleCI/GitHub-кольцо) ловит бот-блок YouTube
(«Sign in to confirm you're not a bot»). convert1s САМ тянет YouTube на своих
серверах, а мы забираем готовый MP3 с его CDN — наш IP YouTube не касается, блок
обходится. Тот же приём, что tubetranscript для титров, только для аудио.

ПОТОК (разобран из HAR DumpsAudio/media.ytmp3.gg.har):
  1. POST hub.convert1s.com/api/download  {url, output:{type:audio,format:mp3}, ...}
     → {statusUrl: "https://vps-*.<домен>/api/status/{id}?token=…", title, duration}
  2. GET statusUrl (поллинг) → {status:completed, downloadUrl: ".../stream/{id}?token=…"}
  3. GET downloadUrl → mp3
Домен статуса/стрима ДИНАМИЧЕСКИЙ (из ответа, не хардкодить). Если vps-домен режет
наш IP — фолбэк через Worker-релей CRAWL_PROXY (config). Без капчи/Turnstile.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

_API = "https://hub.convert1s.com/api/download"
_H = {"Origin": "https://media.ytmp3.gg", "Referer": "https://media.ytmp3.gg/",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}


def _via(url: str, relay: bool) -> str:
    """Прямой URL или через Cloudflare Worker-релей (чистый edge-egress)."""
    if relay and config.CRAWL_PROXY:
        u = config.CRAWL_PROXY.rstrip("/") + "/?url=" + urllib.parse.quote(url, safe="")
        if config.CRAWL_PROXY_KEY:
            u += "&k=" + urllib.parse.quote(config.CRAWL_PROXY_KEY, safe="")
        return u
    return url


def _get_json(url: str, relay: bool, timeout: int = 40) -> dict:
    req = urllib.request.Request(_via(url, relay), headers=_H)
    with urllib.request.urlopen(req, timeout=timeout) as x:
        return json.loads(x.read().decode("utf-8", errors="replace"))


def fetch_mp3(video_id: str, workdir: Path | None = None,
              poll_timeout: int = 300, to_16k_mono: bool = True) -> Path:
    """video_id → путь к mp3 (по умолчанию 16кГц моно для Whisper). Кидает при сбое."""
    workdir = Path(workdir or tempfile.mkdtemp(prefix="asr_"))
    body = json.dumps({"url": f"https://www.youtube.com/watch?v={video_id}",
                       "os": "windows",
                       "output": {"type": "audio", "format": "mp3"},
                       "audio": {"bitrate": "128k"}}).encode()
    req = urllib.request.Request(_API, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **_H})
    with urllib.request.urlopen(req, timeout=60) as x:
        resp = json.loads(x.read().decode("utf-8", errors="replace"))
    surl = resp.get("statusUrl")
    if not surl:
        raise RuntimeError(f"convert1s: нет statusUrl ({str(resp)[:200]})")

    # поллинг: прямой, при не-JSON/сбое — через релей; транзиентные ошибки ретраим
    dl = None
    for _ in range(max(1, poll_timeout // 3)):
        st = None
        for relay in (False, True):
            try:
                st = _get_json(surl, relay)
                break
            except Exception:  # noqa: BLE001 — не-JSON во время инициализации джобы
                st = None
        if st is None:
            time.sleep(3); continue
        status = st.get("status")
        if status == "completed":
            dl = st.get("downloadUrl"); break
        if status in ("error", "failed"):
            raise RuntimeError(f"convert1s: конвертация не удалась ({str(st)[:200]})")
        time.sleep(3)
    if not dl:
        raise RuntimeError("convert1s: таймаут ожидания конвертации")

    raw = workdir / f"{video_id}.src.mp3"
    for relay in (False, True):
        try:
            req = urllib.request.Request(_via(dl, relay), headers=_H)
            with urllib.request.urlopen(req, timeout=240) as x, open(raw, "wb") as f:
                shutil.copyfileobj(x, f)
            if raw.stat().st_size >= 2000:
                break
        except Exception:  # noqa: BLE001
            continue
    if not raw.exists() or raw.stat().st_size < 2000:
        raise RuntimeError("convert1s: пустой/битый файл")

    if not to_16k_mono:
        return raw
    # ffmpeg -> 16кГц моно (мельче файл для Whisper); нет ffmpeg -> отдаём как есть
    out = workdir / f"{video_id}.mp3"
    try:
        p = subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1",
                            str(out)], capture_output=True, text=True, timeout=180)
        if p.returncode == 0 and out.exists() and out.stat().st_size > 1000:
            raw.unlink(missing_ok=True)
            return out
    except Exception:  # noqa: BLE001
        pass
    return raw
