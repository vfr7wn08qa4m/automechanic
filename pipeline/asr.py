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
import shutil
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


# CF Whisper режет большие тела: 2.87МБ (16 мин) -> 413 Payload Too Large,
# 879КБ (5 мин) -> 200 OK. Поэтому длинное аудио режем на куски по CF_CHUNK_SEC
# и склеиваем со сдвигом таймкодов. 5 мин при 16кГц моно ≈ 0.9МБ — с запасом.
CF_CHUNK_SEC = int(os.getenv("CF_CHUNK_SEC", "300"))
_CF_MAX_BYTES = int(os.getenv("CF_MAX_BYTES", str(1_000_000)))


def _audio_duration(audio: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float((p.stdout or "0").strip())
    except ValueError:
        return 0.0


def _cloudflare(audio: Path) -> list[tuple[int, str]]:
    """Whisper на Cloudflare. Большой файл — по кускам (см. CF_CHUNK_SEC),
    таймкоды каждого куска сдвигаются на его начало."""
    if audio.stat().st_size <= _CF_MAX_BYTES:
        return _cf_one(audio)
    dur = _audio_duration(audio)
    if not dur:
        return _cf_one(audio)                     # длительность не узнали — как есть
    lines: list[tuple[int, str]] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="cfchunk_"))
    try:
        offset = 0
        while offset < dur:
            part = tmpdir / f"p{offset}.mp3"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                            "-ss", str(offset), "-t", str(CF_CHUNK_SEC), "-i", str(audio),
                            "-ar", "16000", "-ac", "1", str(part)],
                           capture_output=True, timeout=300)
            if part.exists() and part.stat().st_size > 1000:
                for sec, text in _cf_one(part):
                    lines.append((sec + offset, text))
                part.unlink(missing_ok=True)
            offset += CF_CHUNK_SEC
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return lines


def _cf_one(audio: Path) -> list[tuple[int, str]]:
    """Один POST в CF Whisper (тело должно быть в пределах лимита).

    РОТАЦИЯ АККАУНТОВ. У каждого аккаунта Cloudflare 10k нейронов в сутки на
    ВСЁ, и Whisper дороже эмбеддинга на порядки — первый аккаунт выгорает быстро.
    Раньше здесь был жёстко один config.CF_ACCOUNT_ID: на его 429 весь ASR-проход
    падал, хотя в cf_tokens.txt лежат ещё аккаунты (2026-07-30 в кольце именно
    так и встало: «cloudflare: 429 Too Many Requests» на каждом видео).
    Теперь берём тот же пул, что и эмбеддинг (embed._load_cf_accounts), и на
    401/429 идём к следующему аккаунту.
    """
    from .embed import _load_cf_accounts
    accounts = _load_cf_accounts()
    if not accounts:
        raise RuntimeError("CF_ACCOUNT_ID/CF_API_TOKEN не заданы")

    errors: list[str] = []
    body = None
    for idx, (acc_id, token) in enumerate(accounts, 1):
        url = (f"https://api.cloudflare.com/client/v4/accounts/"
               f"{acc_id}/ai/run/@cf/openai/whisper")
        r = requests.post(url, data=audio.read_bytes(),
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/octet-stream"},
                          timeout=300)
        if r.status_code in (401, 429):
            errors.append(f"аккаунт #{idx}: HTTP {r.status_code}")
            print(f"[asr] CF аккаунт #{idx}/{len(accounts)}: HTTP {r.status_code}"
                  f" — пробую следующий")
            continue
        r.raise_for_status()
        body = r.json()
        break
    if body is None:
        raise RuntimeError("cf whisper: все аккаунты исчерпаны ("
                           + ", ".join(errors) + ")")
    if not body.get("success"):
        raise RuntimeError(f"cf whisper error: {body.get('errors')}")
    result = body["result"]
    words = result.get("words") or []
    if words:  # группируем слова в строки по ~10 секунд
        lines: list[tuple[int, str]] = []
        bucket_start: float | None = None
        bucket: list[str] = []
        for w in words:
            # ВАЖНО: CF отдаёт start/end иногда как null — .get(k, 0) вернёт None
            # (ключ есть!), и арифметика падает. Нормализуем через `or 0.0`.
            w_start = w.get("start") or 0.0
            w_end = w.get("end") or w_start
            if bucket_start is None:
                bucket_start = w_start
            bucket.append(w.get("word", ""))
            if w_end - bucket_start >= 10:
                lines.append((int(bucket_start), " ".join(bucket).strip()))
                bucket_start, bucket = None, []
        if bucket:
            lines.append((int(bucket_start or 0), " ".join(bucket).strip()))
        return [(s, t) for s, t in lines if t]
    return [(0, result.get("text", "").strip())]


def _local(audio: Path) -> list[tuple[int, str]]:
    from faster_whisper import WhisperModel  # ленивый тяжёлый импорт
    model = WhisperModel(ASR_LOCAL_MODEL, device="auto", compute_type="auto")
    segments, _info = model.transcribe(str(audio), vad_filter=True)
    return [(int(s.start), s.text.strip()) for s in segments if s.text.strip()]


def transcribe_file(audio: Path) -> list[tuple[int, str]]:
    """ASR из УЖЕ скачанного локального аудио (YouTube-фолбэк: аудио тянет yt-dlp
    дома, т.к. Kaggle/облако YouTube режет). Провайдеры cloudflare (free) / local
    (faster-whisper); 'remote' пропускаем — он принимает media_url, не файл."""
    errors = []
    for name in ASR_PROVIDERS:
        if name == "remote":
            continue
        try:
            if name == "cloudflare":
                return _cloudflare(audio)
            if name == "local":
                return _local(audio)
            errors.append(f"{name}: unknown")
        except ImportError:
            errors.append(f"{name}: faster-whisper не установлен")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {str(e)[:150]}")
    raise RuntimeError("ASR (file) недоступен: " + " | ".join(errors))


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
