# Карта бесплатных ИИ-провайдеров и лимитов

Выжимка из твоих наработок (`ClaudeCode/clawbot/src/runtime_config.py`,
`api_providers.py`, `Free/*.txt`) применительно к этому проекту.
Все Tier-1 — OpenAI-совместимые: меняются только `DISTILL_BASE_URL/KEY/MODEL`.

## Tier 1 — официальные бесплатные ключи (основа дистилляции)

| Провайдер | Base URL | Модели под дистилляцию | Лимиты |
|---|---|---|---|
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | ✅ minimax-m2.7 (проверена на реальном кейсе), ✅ nemotron-3-super; ❌ qwen3-coder-480b, glm-5.1 — EOL 410 | ~40 RPM на ключ; **модели смертны — сверять build.nvidia.com** |
| Groq | `https://api.groq.com/openai/v1` | llama-3.3-70b-versatile, gpt-oss-120b | ~8k TPM/req, дневной TPD |
| Cerebras | `https://api.cerebras.ai/v1` | qwen-3-235b | 5 RPM, 1M ток/день |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | gemini-flash-latest | 15 RPM / 1500 req/день |
| OpenRouter | `https://openrouter.ai/api/v1` | любые `:free` | дневной кап free-моделей |

Стратегия объёма: каскад с ротацией ключей и кулдаунами — готовый паттерн в
`C:\aa\ClaudeCode\clawbot\src\api_providers.py` (`_OpenAICompatProvider`).
На 1 видео ≈ 15-25k токенов входа → Cerebras 1M ток/день ≈ 40-60 видео/день
с одного ключа; NIM + Groq + Gemini параллельно → сотни видео/день бесплатно.

## Эмбеддинги (мультиязычные!)

| Провайдер | Модель | Размерность | Статус на 2026-07-10 |
|---|---|---|---|
| Локально (build-агент) | `sentence-transformers` BAAI/bge-m3, CPU ок | 1024 | ✅ **primary** (без лимитов) |
| Cloudflare Workers AI | `@cf/baai/bge-m3` | 1024 | реплика, 10k neurons/день |
| NVIDIA NIM | `baai/bge-m3` | 1024 | ❌ отдаёт 500 (сломан у них) |
| NVIDIA NIM | `nvidia/nv-embedqa-e5-v5` | 1024 | ✅ работает, но англо-центричен — не для мультиязыка |
| NVIDIA NIM | `nvidia/llama-3.2-nv-embedqa-1b-v2` | — | ❌ 410 EOL |

Важно: **одна и та же модель во всех репликах** — иначе векторы несравнимы.
bge-m3: 100+ языков, кросс-языковой поиск из коробки (запрос RU → кейс ES).

`nvidia/nv-embedcode-7b-v1` из твоего `embed_test.py` — НЕ подходит
(заточена под код, не под мультиязычный текст).

## Транскрипция без титров (фаза 3+, опционально)

- NVIDIA NIM Riva: `openai/whisper-large-v3` (gRPC) — паттерн уже есть в
  `nvidea/free-claude-code` (`WHISPER_DEVICE=nvidia_nim`).
- Локально: faster-whisper на твоей GPU — без лимитов, медленнее.

Экономика: видео без титров ~10-20% (у YouTube авто-титры почти всегда есть);
откладываем до тех пор, пока не кончатся видео С титрами.

## Чаты из твоего арсенала (Tier 2/3) — слой ответов бекэнда + резерв дистилляции

Главное назначение (по замыслу юзера): **формирование ответа мобилке на
бекэнде** — RAG в `backend/answer.py`, каскад `ANSWER_ENDPOINTS` (по умолчанию
Pollinations, проверен без ключа). Второе — резерв каскада дистилляции.

Инвентарь (из обследования C:\aa\ClaudeCode; код-обёртки уже есть в
`clawbot/src/*_provider.py`):

| Чат | Модели | Механика | Хрупкость |
|---|---|---|---|
| Pollinations | gpt-oss-20b и др. | **OpenAI-совместимый, БЕЗ ключа** — ✅ проверен 2026-07-10, работает | низкая — можно прямо в CI |
| chat-gpt.org | OpenRouter-модели (claude-haiku, grok-mini...) | CSRF + SSE, чистый requests | средняя |
| Qwen (chat.qwen.ai) | qwen3.6-plus | bx-ua/bx-umidtoken одноразовые → нужен Playwright | высокая |
| Z.ai (chat.z.ai) | GLM-5 | реверснутая X-Signature + JWT | высокая |
| DeepSeek (chat) | V3 | Cloudflare PoW через wasm + userToken | высокая |
| Grok, DuckAI, Mistral, LiteGPT, api.airforce, chatgptfree | разные | cookies/анти-бот/1 RPM | высокая |

Роль в конвейере: **хвост каскада DISTILL_ENDPOINTS**, когда дневные квоты
Tier-1 исчерпаны. Браузерные (Qwen/Z.ai/Grok/DeepSeek) в CircleCI-докер не
затащить — они живут ТОЛЬКО через гейтвей на твоём домашнем docker-сервере
(clawbot-portal на 192.168.1.14 уже стоит): выставить OpenAI-совместимый
endpoint + туннель (cloudflared/ngrok) и вписать последним эндпоинтом каскада.
Домашняя машина при этом остаётся «только мониторинг» — сервер и так работает.

Честные ограничения чатов для дистилляции: ~1 RPM, обрезка длинных
транскриптов, нет JSON-mode (парсим сами, ретраи), ломаются без предупреждения.
Поэтому: Tier-1 API — основа, чаты — ночной резерв. Каждый кейс хранит
`distill_model` — что извлечено слабой моделью, потом пере-дистиллируем сильной.

## Транскрипты из облачного CI (без чистого IP)

Проверено 2026-07-10: публичные Invidious-зеркала листят дорожки, но тела
титров отдают пустыми (YouTube блокирует их бекенды для timedtext) — держим в
цепочке как бесплатный лотерейный билет, не как опору.

| Путь | Цена | Роль |
|---|---|---|
| yt-dlp + **residential-прокси** (webshare.io и др.) | ~$1-3/GB; титры ~100KB → **5-10 тыс. видео с 1 GB** | основной рабочий путь в CI |
| Supadata / аналоги (transcript-API) | free ~100 req, дальше от ~$0.001-0.01/видео | смоук + запасной |
| Invidious-зеркала | бесплатно | оппортунистический бонус, инстансы курировать |
| yt-dlp с чистого IP | бесплатно | если когда-нибудь появится мини-ПК/VPS с резидентным IP |

## Хранилища

| Сервис | Free | Использование |
|---|---|---|
| Cloudflare R2 | 10 GB, egress $0 | архив #1 (титры+кейсы) |
| Backblaze B2 | 10 GB | архив #2 (реплика rclone'ом) |
| Qdrant Cloud | 1 GB кластер | вектора #1 (~200k кейсов) |
| Oracle Autonomous DB 23ai | 2×20 GB, AI Vector Search | вектора #2 + реляционка бекэнда |
| Oracle ARM VM | 4 OCPU / 24 GB RAM | FastAPI + всё остальное |

Оценка места: 1 кейс ≈ 3-6 KB JSON + 4 KB вектор; титры ≈ 50-200 KB VTT.
10 GB R2 ≈ 60-100k видео с титрами. До 100k кейсов бесплатных тиров хватает.
