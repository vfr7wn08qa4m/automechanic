"""Конфигурация пайплайна: всё через переменные окружения / .env.

Ни одного секрета в коде. Скопируй .env.example -> .env и заполни.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _get(*names: str, default: str | None = None) -> str | None:
    """Первое непустое значение из перечисленных имён env-переменных."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


# --- LLM для дистилляции (OpenAI-совместимый endpoint) -----------------------
# По умолчанию NVIDIA NIM; подойдёт и локальный free-claude-code gateway,
# DeepSeek, Ollama и т.д. — меняются только BASE_URL/KEY/MODEL.
DISTILL_BASE_URL = _get("DISTILL_BASE_URL", default="https://integrate.api.nvidia.com/v1")
DISTILL_API_KEY = _get("DISTILL_API_KEY", "NIM_API_KEY", "NVIDIA_NIM_API_KEY")
# minimax-m2.7 проверена на реальном кейсе 2026-07-10; nemotron-3-super тоже жив.
# ВНИМАНИЕ: у моделей NIM есть EOL (410 Gone) — список сверять на build.nvidia.com
DISTILL_MODEL = _get("DISTILL_MODEL", default="minimaxai/minimax-m2.7")

# Каскад дистилляции: точки с запятой между эндпоинтами,
# внутри "base_url|ИМЯ_ENV_С_КЛЮЧОМ_или_none|model". Первый — основной,
# следующие пробуются при 429/402/410/5xx/таймаутах. Пример в .env.example.
DISTILL_ENDPOINTS = _get("DISTILL_ENDPOINTS")
DISTILL_MAX_INPUT_CHARS = int(_get("DISTILL_MAX_INPUT_CHARS", default="60000"))
# reasoning-моделям (minimax, deepseek-r1...) нужен запас на "размышления"
DISTILL_MAX_TOKENS = int(_get("DISTILL_MAX_TOKENS", default="8192"))
DISTILL_TIMEOUT_SECONDS = int(_get("DISTILL_TIMEOUT_SECONDS", default="240"))

# --- Эмбеддинги ---------------------------------------------------------------
# bge-m3: мультиязычная (100+ языков), 1024 измерения.
# Primary: NVIDIA NIM; fallback: Cloudflare Workers AI — та же модель,
# поэтому векторы совместимы между репликами.
EMBED_BASE_URL = _get("EMBED_BASE_URL", default="https://integrate.api.nvidia.com/v1")
EMBED_API_KEY = _get("EMBED_API_KEY", "NIM_API_KEY", "NVIDIA_NIM_API_KEY")
EMBED_MODEL = _get("EMBED_MODEL", default="baai/bge-m3")
EMBED_DIM = int(_get("EMBED_DIM", default="1024"))

CF_ACCOUNT_ID = _get("CF_ACCOUNT_ID")
CF_API_TOKEN = _get("CF_API_TOKEN")
CF_EMBED_MODEL = _get("CF_EMBED_MODEL", default="@cf/baai/bge-m3")

# Свой эмбеддинг-сервер (напр. bge-m3 на Kaggle T4, см. kaggle/embedding_server.py).
# Если задан — идёт первым в цепочке провайдеров.
EMBED_REMOTE_URL = _get("EMBED_REMOTE_URL")     # напр. https://xxxx.ngrok-free.app
EMBED_REMOTE_KEY = _get("EMBED_REMOTE_KEY")

# --- YouTube ------------------------------------------------------------------
YOUTUBE_API_KEY = _get("YOUTUBE_API_KEY")  # без ключа discovery работает через yt-dlp
SUB_LANGS = _get("SUB_LANGS", default="ru,uk,en,de,es,pt,pl,tr").split(",")
YTDLP_SLEEP_SECONDS = float(_get("YTDLP_SLEEP_SECONDS", default="8"))
# residential-прокси для yt-dlp в CI (http://user:pass@host:port или socks5://...)
YTDLP_PROXY = _get("YTDLP_PROXY")

# --- Azure DevOps -------------------------------------------------------------
# Кастомный процесс AutoMechanikBoard (проверено вживую 2026-07-11):
#   Task    — видео/тред. Состояния System.State: New -> ReadyForFilter ->
#             ReadyForEmbeding -> Closed (Removed = брак/offtopic; Active — резерв).
#   Epic    — канал/форум. Курирование каналов тегами state:active|paused.
#   Custom.url — поле со ссылкой на источник (для дедупа/дельта-поиска).
ADO_ORG = _get("ADO_ORG")            # https://dev.azure.com/{ADO_ORG}
ADO_PROJECT = _get("ADO_PROJECT")
ADO_PAT = _get("ADO_PAT")
ADO_WORKITEM_TYPE = _get("ADO_WORKITEM_TYPE", default="Task")    # видео/тред
ADO_CHANNEL_TYPE = _get("ADO_CHANNEL_TYPE", default="Epic")      # канал/форум
ADO_URL_FIELD = _get("ADO_URL_FIELD", default="Custom.url")      # поле ссылки
ADO_STATE_PREFIX = "state:"          # теги курирования каналов (Epic)
# Бюджет минут CircleCI хранится В ADO (стейт между прогонами без внешних
# зависимостей): помесячный «леджер» на аккаунт — неиспользуемый тип Feature,
# минуты в числовом поле Effort. Свежий work item каждый месяц (ревизии не
# копятся к лимиту 10k). CI_BUDGET_STORE=ado|r2 (по умолч. ado).
ADO_BUDGET_TYPE = _get("ADO_BUDGET_TYPE", default="Feature")
ADO_BUDGET_FIELD = _get("ADO_BUDGET_FIELD", default="Microsoft.VSTS.Scheduling.Effort")

# --- Хранилища ----------------------------------------------------------------
# S3-совместимое (Cloudflare R2 / Backblaze B2) — архив титров и кейсов.
S3_ENDPOINT = _get("S3_ENDPOINT")            # напр. https://<acc>.r2.cloudflarestorage.com
S3_KEY = _get("S3_KEY", "S3_ACCESS_KEY_ID")
S3_SECRET = _get("S3_SECRET", "S3_SECRET_ACCESS_KEY")
S3_BUCKET = _get("S3_BUCKET", default="automech-archive")

# Qdrant Cloud (free 1GB) — векторная реплика для поиска.
QDRANT_URL = _get("QDRANT_URL")              # напр. https://xyz.cloud.qdrant.io:6333
QDRANT_API_KEY = _get("QDRANT_API_KEY")
QDRANT_COLLECTION = _get("QDRANT_COLLECTION", default="cases")
