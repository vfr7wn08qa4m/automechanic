"""ASR (речь -> текст) для видео без титров (CarCareKiosk и т.п.).

Цепочка провайдеров:
1. Cloudflare Workers AI `@cf/openai/whisper` — REST, бесплатный тир,
   аудио до ~25MB (наши 1-4 МБ mp3 проходят). Нужны CF_ACCOUNT_ID/CF_API_TOKEN.
2. Локально faster-whisper (pip install faster-whisper) — модель из
   ASR_LOCAL_MODEL (tiny/base/small/large-v3). Для продакшна — large-v3 на
   Kaggle T4 (добавить /transcribe в kaggle/embedding_server.py) или CF.

Аудио извлекается ffmpeg'ом прямо из URL (16 kHz mono mp3) — видео целиком
на диск не пишем.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import requests

from . import config

ASR_LOCAL_MODEL = os.getenv("ASR_LOCAL_MODEL", "small")
ASR_PROVIDERS = [p.strip() for p in os.getenv(
    "ASR_PROVIDERS", "remote,cloudflare,local").split(",") if p.strip()]


def _remote(media_url: str) -> list[tuple[int, str]]:
    """Kaggle T4 сервер: POST /transcribe {"media_url"} (whisper large-v3).
    Аудио качает сам Kaggle — с этой стороны трафика нет."""
    if not config.EMBED_REMOTE_URL:
        raise RuntimeError("EMBED_REMOTE_URL не задан")
    headers = {"Content-Type": "application/json"}
    if config.EMBED_REMOTE_KEY:
        headers["X-Api-Key"] = config.EMBED_REMOTE_KEY
    r = requests.post(f"{config.EMBED_REMOTE_URL.rstrip('/')}/transcribe",
                      json={"media_url": media_url}, headers=headers, timeout=900)
    r.raise_for_status()
    return [(int(sec), text) for sec, text in r.json()["lines"]]


def audio_from_url(media_url: str, max_minutes: int = 15) -> Path:
    """Вытянуть аудио-дорожку по HTTP в 16kHz mono mp3 (маленький файл)."""
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="asr_")
    os.close(fd)  # иначе Windows держит файл и unlink после работы падает
    out = Path(path)
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-t", str(max_minutes * 60), "-i", media_url,
           "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0 or not out.stat().st_size:
        raise RuntimeError(f"ffmpeg failed: {p.stderr[-300:]}")
    return out


def _cloudflare(audio: Path) -> list[tuple[int, str]]:
    if not (config.CF_ACCOUNT_ID and config.CF_API_TOKEN):
        raise RuntimeError("CF_ACCOUNT_ID/CF_API_TOKEN не заданы")
    url = (f"https://api.cloudflare.com/client/v4/accounts/"
           f"{config.CF_ACCOUNT_ID}/ai/run/@cf/openai/whisper")
    r = requests.post(url, data=audio.read_bytes(),
                      headers={"Authorization": f"Bearer {config.CF_API_TOKEN}",
                               "Content-Type": "application/octet-stream"},
                      timeout=300)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"cf whisper error: {body.get('errors')}")
    result = body["result"]
    words = result.get("words") or []
    if words:  # группируем слова в строки по ~10 секунд
        lines: list[tuple[int, str]] = []
        bucket_start: float | None = None
        bucket: list[str] = []
        for w in words:
            if bucket_start is None:
                bucket_start = w.get("start", 0)
            bucket.append(w.get("word", ""))
            if w.get("end", 0) - bucket_start >= 10:
                lines.append((int(bucket_start), " ".join(bucket).strip()))
                bucket_start, bucket = None, []
        if bucket:
            lines.append((int(bucket_start or 0), " ".join(bucket).strip()))
        return lines
    return [(0, result.get("text", "").strip())]


def _local(audio: Path) -> list[tuple[int, str]]:
    from faster_whisper import WhisperModel  # ленивый тяжёлый импорт
    model = WhisperModel(ASR_LOCAL_MODEL, device="auto", compute_type="auto")
    segments, _info = model.transcribe(str(audio), vad_filter=True)
    return [(int(s.start), s.text.strip()) for s in segments if s.text.strip()]


def transcribe_url(media_url: str) -> list[tuple[int, str]]:
    """[(сек, текст)] из видео/аудио по URL. Провайдеры по цепочке.

    remote (Kaggle) качает аудио сам — локальная выжимка ffmpeg'ом нужна
    только для cloudflare/local, поэтому делается лениво."""
    errors = []
    audio: Path | None = None
    try:
        for name in ASR_PROVIDERS:
            try:
                if name == "remote":
                    return _remote(media_url)
                if name in ("cloudflare", "local"):
                    if audio is None:
                        audio = audio_from_url(media_url)
                    return _cloudflare(audio) if name == "cloudflare" else _local(audio)
                errors.append(f"{name}: unknown")
            except ImportError:
                errors.append(f"{name}: faster-whisper не установлен")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {str(e)[:150]}")
        raise RuntimeError("ASR недоступен: " + " | ".join(errors))
    finally:
        if audio is not None:
            try:
                audio.unlink(missing_ok=True)
            except OSError:
                pass  # уборка не должна ронять успешную транскрипцию
