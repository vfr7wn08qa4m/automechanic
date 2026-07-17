# Cloudflare Worker-реле для форум-краула

Зачем: форумы vwvortex/bimmerforums/bobistheoilguy режут плейн-HTTP (IP/бот-фильтр
Cloudflare). Реле гоняет запросы через **чистый Cloudflare-edge egress** с
браузерными заголовками — снимает IP/базовые блоки. (Активный JS-челлендж реле
не решает — но у части форумов блок именно по IP, и его это снимает.) Бонус:
единый чистый egress для всех источников. Бесплатно: 100k запросов/день.

## Деплой (через дашборд, ~3 мин)
1. **dash.cloudflare.com** → слева **Compute (Workers)** → **Workers & Pages** →
   **Create** → **Create Worker**.
2. Имя, напр. `automech-relay` → **Deploy** (пока с дефолтным кодом).
3. **Edit code** → вставь весь `proxy/worker.js` → **Deploy**.
4. **Settings → Variables and Secrets** → **Add**:
   - `PROXY_SECRET` = придумай случайную строку (тип **Secret/Encrypt**) → Save/Deploy.
5. Скопируй URL воркера: `https://automech-relay.<твой-сабдомен>.workers.dev`.

## Проверка вручную (по желанию)
```
https://automech-relay.<sub>.workers.dev/?url=https%3A%2F%2Fbobistheoilguy.com%2Fforums%2F&k=<PROXY_SECRET>
```
200 и HTML форума = реле пробило; 403/202 = сайт за JS-челленджем (нужен браузерный путь).

## Подключение к краулеру
Дай мне **URL воркера + PROXY_SECRET** — я пропишу в `shared_secrets`:
- `CRAWL_PROXY` = URL воркера
- `CRAWL_PROXY_KEY` = PROXY_SECRET

затем `deploy.py --context` (разольёт в оба CI-аккаунта) и перезапущу краул зоны b
через реле + проверю все 3 форума фактически. Локально краул тоже подхватит
(`.env`: `CRAWL_PROXY=...`, `CRAWL_PROXY_KEY=...`).

Краулер уже умеет реле: `pipeline/crawler.py:_fetch()` — если `CRAWL_PROXY` задан,
каждый запрос идёт `{{CRAWL_PROXY}}/?url=<цель>&k=<ключ>`.
