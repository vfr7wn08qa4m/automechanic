"""Kaggle T4: сервер эмбеддингов bge-m3 (+ опционально Whisper) для конвейера.

Это ПРАВИЛЬНОЕ применение Kaggle-GPU в проекте: эмбеддинги и ASR — счётные
задачи, где T4 снимает все лимиты бесплатных API. Дистилляцию (извлечение
смысла из титров) на T4 НЕ переносим: влезает максимум ~14-32B в кванте, что
заметно глупее бесплатных API-моделей класса minimax-m2.7/nemotron-super.

Развёртывание (паттерн тот же, что в твоём SmartScraper):
1. Kaggle -> New Notebook -> Accelerator: GPU T4 x2 -> вставить этот файл одной
   ячейкой (перед ним ячейка:
   !pip install -q fastapi uvicorn sentence-transformers pyngrok faster-whisper)
2. Kaggle Secrets: NGROK_TOKEN (обязательно), EMBED_SERVER_KEY (свой пароль),
   GIST_TOKEN + GIST_ID (опционально — публикация URL для конвейера).
3. Запустить. URL туннеля печатается и (если настроен) пишется в Gist.
4. В CircleCI-контекст: EMBED_REMOTE_URL=<ngrok url>, EMBED_REMOTE_KEY=<пароль>.
   Конвейер сам ходит сюда первым, при падении — Cloudflare/NIM (pipeline/embed.py).

Ограничения Kaggle: сессия ~9-12 ч, 30 GPU-часов/нед -> сервер поднимать на
время больших батчей (индексация бэклога), для «дежурного» режима хватает
Cloudflare (10k neurons/день).

API:
    GET  /health                       -> {"ok": true, "model": "BAAI/bge-m3"}
    POST /embed {"texts": [...], "input_type": "passage|query"}
                                       -> {"vectors": [[...1024 floats...]]}
"""
import json
import os

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from pyngrok import ngrok
from sentence_transformers import SentenceTransformer

# --- Kaggle secrets ---------------------------------------------------------
try:  # в Kaggle секреты берутся так; вне Kaggle — из env
    from kaggle_secrets import UserSecretsClient
    _sec = UserSecretsClient()
    def secret(name: str, default: str = "") -> str:
        try:
            return _sec.get_secret(name)
        except Exception:
            return os.getenv(name, default)
except ImportError:
    def secret(name: str, default: str = "") -> str:
        return os.getenv(name, default)

API_KEY = secret("EMBED_SERVER_KEY", "change-me")

print("Загружаю BAAI/bge-m3 (первый раз ~2.3 GB)...")
model = SentenceTransformer("BAAI/bge-m3", device="cuda")
print("Модель готова.")

# ASR по запросу (для CarCareKiosk и видео без титров):
# в ячейку установки добавь faster-whisper, тогда /transcribe оживёт.
try:
    from faster_whisper import WhisperModel
    asr_model = WhisperModel("large-v3", device="cuda", compute_type="float16")
    print("Whisper large-v3 готов.")
except ImportError:
    asr_model = None
    print("faster-whisper не установлен — /transcribe отключён.")

app = FastAPI()


class EmbedReq(BaseModel):
    texts: list[str]
    input_type: str = "passage"   # для bge-m3 не критично, оставлено для совместимости


@app.get("/health")
def health():
    return {"ok": True, "model": "BAAI/bge-m3", "dim": 1024}


@app.post("/embed")
def embed(req: EmbedReq, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(401, "bad api key")
    if len(req.texts) > 256:
        raise HTTPException(413, "max 256 texts per request")
    vecs = model.encode(req.texts, normalize_embeddings=True,
                        batch_size=32).tolist()
    return {"vectors": vecs}


class TranscribeReq(BaseModel):
    media_url: str            # прямой URL mp4/mp3 — аудио скачивается на Kaggle


@app.post("/transcribe")
def transcribe(req: TranscribeReq, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(401, "bad api key")
    if asr_model is None:
        raise HTTPException(501, "faster-whisper not installed")
    import subprocess, tempfile
    audio = tempfile.mkstemp(suffix=".mp3")[1]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", req.media_url,
                    "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", audio],
                   check=True, timeout=900)
    segments, info = asr_model.transcribe(audio, vad_filter=True)
    return {"lang": info.language,
            "lines": [[int(s.start), s.text.strip()] for s in segments]}


def publish_url_to_gist(url: str) -> None:
    """Опционально: конвейер узнаёт адрес сервера из Gist (как в SmartScraper)."""
    token, gist_id = secret("GIST_TOKEN"), secret("GIST_ID")
    if not (token and gist_id):
        return
    import requests
    requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"files": {"embed_server.json": {
            "content": json.dumps({"url": url})}}},
        timeout=30)
    print(f"URL опубликован в gist {gist_id}")


if __name__ == "__main__":
    ngrok.set_auth_token(secret("NGROK_TOKEN"))
    tunnel = ngrok.connect(8000, "http")
    print(f"\n=== EMBED_REMOTE_URL={tunnel.public_url} ===\n")
    publish_url_to_gist(tunnel.public_url)
    uvicorn.run(app, host="0.0.0.0", port=8000)
