# CircleCI: связка с GitHub, секреты, парные аккаунты

## 1. Связка GitHub ↔ CircleCI

1. Репо пушится на GitHub (пусть будет `automech`). Для парной схемы — см. §3:
   форк/зеркало во второй GitHub-аккаунт.
2. circleci.com → **Sign up with GitHub** → авторизуешь OAuth-приложение.
3. Projects → рядом с репо **Set Up Project** → «Fastest: use the .circleci/config.yml
   in my repo» → ветка `main`.
4. Всё: пуши в репо триггерят пайплайн, расписания настраиваются отдельно (§4).

## 2. Куда заливать токены (по системам)

| Секрет | Где лежит | Путь |
|---|---|---|
| ADO PAT (Work Items R/W) | CircleCI | Organization Settings → **Contexts** → создать контекст `automech` → Environment Variables: `ADO_PAT`, `ADO_ORG`, `ADO_PROJECT` |
| DISTILL_API_KEY (nvapi-...) | CircleCI | тот же контекст `automech` |
| S3 (R2/B2), QDRANT, SUPADATA, YTDLP_PROXY, EMBED_REMOTE_* | CircleCI | тот же контекст `automech` |
| YOUTUBE_API_KEY, ADO_PAT_SECRET, S3_* | Azure DevOps | Pipelines → **Library** → Variable group `automech-secrets`, галка «secret» у каждого |
| NGROK_TOKEN, EMBED_SERVER_KEY, GIST_TOKEN/GIST_ID | Kaggle | Notebook → Add-ons → **Secrets** |
| Всё то же для локальных прогонов | локально | `.env` (в .gitignore) |

Почему контекст, а не Project Settings → Environment Variables: контекст один
на организацию, шарится между проектами и виден в конфиге явной строкой
`context: automech`. В парной схеме контекст создаётся В КАЖДОМ аккаунте
(секреты между аккаунтами CircleCI не шарятся).

ADO PAT делается так: dev.azure.com → User settings (иконка) → Personal access
tokens → New: scope **Work Items Read & Write**. Срок максимум год — поставь
напоминание о ротации.

## 3. Несколько аккаунтов = самораспределение (без чёт/нечёт)

Идея: N GitHub-аккаунтов → N CircleCI-аккаунтов (у каждого свои ~6000 мин/мес)
→ все гоняют ОДИН конвейер по ОДНОЙ очереди ADO. **Партиции чёт/нечёт больше не
нужны** — распределение делает атомарный **claim** (`test` на System.Rev):
любой аккаунт берёт первый незанятый work item, второй его уже не возьмёт.
Добавляй аккаунты свободно — они сами разбирают очередь «кто где свободен».

Каждому аккаунту задай `CI_ACCOUNT` (метка, напр. `a`,`b`,`c`) — под ней
считается его бюджет минут (см. §5) и подписываются claim'ы в History.

`--partition even|odd` в коде оставлен как опция, но по умолчанию `solo`
(без деления) — полагаемся на claim.

Настройка:
1. GitHub-аккаунт №2 → форк репо (или зеркальный пуш `git push mirror main`).
2. CircleCI-аккаунт №2 через Sign up with GitHub №2 → Set Up Project на форке.
3. В обоих аккаунтах создать контекст `automech` с одинаковыми секретами
   (ADO PAT можно один и тот же или по PAT на аккаунт — по PAT'у на аккаунт
   удобнее: в History work item'а видно, кто клеймил).
4. Расписания — следующий раздел.
5. Синк форка: раз в неделю Sync fork кнопкой на GitHub, либо GitHub Action
   в форке (`schedule:` + `gh repo sync`).

## 3.5 Диспетчер: кто триггерит CircleCI

Триггерит не расписание CircleCI, а **ADO dispatch-hourly** (`controller
dispatch`, раз в час :30): смотрит очередь Task'ов по состояниям и дёргает
CircleCI API у аккаунтов, распределяя прогоны round-robin по остатку бюджета
минут. Поэтому на стороне CircleCI scheduled trigger для conveyor НЕ нужен —
только для crawl (форумы, по зонам).

Аккаунты диспетчер берёт из `CI_ACCOUNTS_JSON` (variable group automech-secrets),
JSON-массив:
```json
[{"name":"acc-a","circleci_token":"CCIPAT-...","circleci_project_slug":"circleci/<orgId>/<projId>","circleci_definition_id":"<uuid>"},
 {"name":"acc-b", ...}]
```
`definition_id` есть → новый API `/pipeline/run`; иначе legacy `/pipeline`.
Пульт accounts.json (`accounts[]`) → deploy раздаёт это в variable group.
Добавляешь аккаунт — просто дописываешь элемент; диспетчер сам начнёт его
использовать, когда у него есть бюджет.

## 4. Расписание (без партиций)

CircleCI: Project Settings → **Triggers** → Add Scheduled Trigger.

- **Конвейер** (титры/дистилляция/индекс): `flow=conveyor`. Достаточно
  запускать раз в 30-60 мин; claim не даст аккаунтам столкнуться. Смещай
  старты аккаунтов на пару минут, чтобы не бить ADO API синхронно.
- **Краул форумов**: `flow=crawl`, `crawl-zone=a|b|c`. Тут зона нужна (это про
  вежливость к ХОСТУ, а не про распределение work items): один аккаунт = одна
  зона, чтобы один IP не молотил один форум. Аккаунт A → zone a (RU),
  аккаунт B → zone b (EN).

Частоту поднимай по росту очереди. Бюджет-гард (§5) сам остановит аккаунт,
у которого кончились минуты, — переусердствовать с расписанием не страшно.

## 5. Бюджет минут (не жечь лимит) — стейт В ADO

`pipeline/ci_budget.py`: каждый CI-этап в начале резервирует минуты через
`guard()`. Если использовано+оценка > `CI_MONTHLY_MINUTES` (env, по умолч. 6000)
— этап тихо выходит (exit 0), не тратя минуты. В конце уточняет фактом. Аккаунт
сам замолкает на остаток месяца и оживает 1-го числа.

**Где хранится остаток (CI_BUDGET_STORE, по умолч. `ado`):** в самом Azure
DevOps — помесячный **work item-леджер на аккаунт** (неиспользуемый тип
**Feature**, минуты в числовом поле **Effort**), title
`[automech-budget:{account}:{YYYY-MM}]`, тег `automech-budget`. Один пайплайн
между прогонами читает/пишет стейт там — без внешних зависимостей. Свежий
work item каждый месяц ⇒ ревизии не упираются в лимит 10k. Диспетчер читает
`remaining()` каждого аккаунта и грузит туда, где свободно = авто-распределение
между X аккаунтами. Гонки записи (два джоба одного аккаунта) сняты rev-test +
ретраем. Фолбэк `CI_BUDGET_STORE=r2` — файл в бакете.

Видно прямо в Boards: query `Work Item Type = Feature AND Tags Contains
automech-budget` → Effort = израсходованные минуты по каждому аккаунту.

В контекст `automech` добавь: `CI_ACCOUNT` (уникальная метка аккаунта),
опц. `CI_MONTHLY_MINUTES`, `CI_BUDGET_STORE`.

Мониторинг с домашней машины (ярлык на рабочий стол а-ля Ring board):
- CircleCI: https://app.circleci.com/pipelines — статусы прогонов обоих аккаунтов;
- ADO: сохранённый query «Tags Contains state:failed» — доска ошибок;
- ничего запускать локально не требуется.
