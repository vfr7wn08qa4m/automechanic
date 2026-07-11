# Скан HAR-дампов из dump/ (2026-07-10)

11 источников. Куки во всех дампах = 0 → страницы читаются **без логина**
(парсинг проще; логин/HAR понадобится только за пейволлами). Вердикт и способ
извлечения по каждому:

| Источник | Язык | Движок | Как парсить | Статус |
|---|---|---|---|---|
| **carmasters.org** (3 HAR) | RU/UA | IPS (Invision) | `forums.py` engine=ipb | ✅ работает: вытащен кейс BMW E46 «лампа АКБ, ошибки 28D7/27DA генератора» |
| **drive2.ru** (3 HAR) | RU | свой CMS | `forums.py` custom `[itemprop=articleBody]` | ✅ страница `/c/{id}` (бортжурнал) даёт чистую запись: «SWM коробка уходила в аварию… заклинил подшипник». `/communities/` — индекс, не запись |
| **vwvortex.com** (2 HAR) | EN | XenForo | `forums.py` engine=xenforo | ✅ 55 содержательных постов из треда |
| **bimmerforums.com** | EN | vBulletin | `forums.py` engine=vbulletin | ✅ движок опознан (в дампе только индекс — нужен URL треда) |
| **bobistheoilguy.com** | EN | XenForo | `forums.py` engine=xenforo | ✅ движок опознан (в дампе индекс форума) |
| **opinautos.com** (2 HAR) | ES | свой Q&A | `forums.py` custom `.PostText/.AnswerText` | ⚙️ селекторы заведены, проверить на реальной странице вопроса |
| **motor-talk.de** | DE | свой + **GraphQL** | POST `/graphql` (не HTML!) | ⚙️ подтверждён GraphQL-эндпоинт (в дампе query `WhoAmI`) — треды тянуть запросами, не парсить HTML |
| **reddit.com** (2 HAR) | EN | Reddit | OAuth API (script app) | ⚠️ проверено: `.json` без ключа = **HTTP 403** (Reddit прикрыл). Нужен OAuth-клиент (бесплатный script-app, 100 req/min) или парс HTML из твоих HAR |
| **club.autohome.com.cn** | ZH | свой CMS | нужен свой экстрактор | ⚙️ китайский, отложить (объём рынка огромный, но ниже приоритет) |

Пустой/битый (переэкспортируй при желании):
- `www.motor-talk.de_2.har` — 0 байт;
- `www.reddit.com_1.har` — обрезан на экспорте (JSON не закрыт), но reddit всё
  равно берём через API, не через HAR.

## Что уже в коде

`pipeline/forums.py` покрывает: IPS, XenForo, vBulletin, phpBB + кастомные
drive2/opinautos + эвристический фолбэк. `extract_posts(html, host)` сам
выбирает путь по хосту/движку.

## Приоритет обработки (по ценности × готовности)

1. **drive2.ru** — RU-бортжурналы, живые кейсы с фото/пробегом, экстрактор готов.
2. **carmasters.org** — RU/UA мастера, IPS готов, кейсы прямо диагностические.
3. **vwvortex + bimmerforums + bobistheoilguy** — EN, движки готовы, нужны URL тредов.
4. **motor-talk.de** — DE, через GraphQL (отдельный клиент ~30 строк).
5. **reddit** — EN, через JSON API (отдельный клиент ~30 строк).
6. **opinautos** — ES, доверить селекторы после проверки на странице вопроса.
7. **autohome.com.cn** — ZH, позже.

## Как эти источники входят в конвейер

Тред/запись → `forums.py` (или GraphQL/JSON-клиент) → та же дистилляция
(`Source.type=forum`) → Epic форума `[forum:host]` → child-кейсы. Discovery
форумных тредов — либо из твоих HAR (список URL), либо обход раздела форума по
страницам. Вежливо: robots.txt, пауза, честный UA (см. forums.py).
