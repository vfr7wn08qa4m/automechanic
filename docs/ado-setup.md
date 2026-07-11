# Настройка Azure DevOps под конвейер

Куда какие токены заливать по всем системам — сводная таблица в
[circleci-setup.md](circleci-setup.md) §2.

## Минимум (работает с кодом из репо как есть)

1. Организация + проект (process: **Basic** — самый простой).
2. PAT: User settings → Personal access tokens → scope **Work Items (Read & Write)**
   + **Build (Read & Execute)** для запуска пайплайнов. В `.env` → `ADO_PAT`.
3. В `.env`: `ADO_ORG` (имя организации из URL), `ADO_PROJECT`,
   `ADO_WORKITEM_TYPE=Issue` (в Basic-процессе).

## Иерархия = «база данных»

```
Epic  [ch:UC1x2y...] УСПЕШНЫЙ АВТОЭЛЕКТРИК     kind:channel  тег state:active
  ├─ Task [vid:NoXmGWC0lkI] ХОВО не заводится…   State=Closed          Custom.url=…
  ├─ Task [vid:GNR4KYLKtAE] Весь мой секрет…     State=ReadyForEmbeding Custom.url=…
  └─ Task [vid:aBcDeF12345] …                    State=New             Custom.url=…
Epic  [forum:bmwclub.example] bmwclub               kind:forum  тег state:active
  └─ Task [vid:forum-8812736450] тема: не работает ЦЗ…  State=ReadyForFilter
```

- **Канал/форум = Epic** (`ADO_CHANNEL_TYPE=Epic`). Маркер `[ch:…]`/`[forum:…]`,
  теги `kind:channel|forum` + `state:active|paused` (курирование каналов — тегом).
- **Видео/тред = Task** (`ADO_WORKITEM_TYPE=Task`) с parent-связью на свой
  Epic-чанк, конвейер по **System.State** (см. таблицу ниже), ссылка в
  **Custom.url**.
- **Курирование**: мусорному каналу ставишь тег `state:paused` (убираешь
  `state:active`) — weekly-синк его пропускает.
- Discovery создаёт Epic-чанки и Task'и в **New**; дельта — по Custom.url
  (`exists_url`) и по детям чанков.

Состояния Task (реальный System.State ↔ логическое имя в коде через STATE_MAP):

| В коде | System.State | Стадия |
|---|---|---|
| new | **New** | найденная дельта (discovery) |
| subs | **ReadyForFilter** | транскрипт в R2 → фильтр Клодом |
| distilled | **ReadyForEmbeding** | кейс извлечён → эмбеддинг |
| indexed | **Closed** | проиндексировано |
| failed/offtopic | **Removed** | брак/не по теме (+ тег-причина) |

## Шардинг крупных каналов (лимит 1000 связей)

ADO держит максимум **1000 связей на work item** → у одного Epic не больше 1000
детей. Крупные каналы и особенно CarCareKiosk-марки (тысячи задач по моделям)
это пробьют. Решение — **цепочка чанков**:

```
[ch:UCxxx#1] Канал            (≤900 детей)  --Successor-->
[ch:UCxxx#2] Канал (chunk 2)  (≤900 детей)  --Successor-->
[ch:UCxxx#3] Канал (chunk 3)  (наполняется)
```

- Порог `CHUNK_CAP=900` (запас под 1000). Новые видео идут в последний
  незаполненный чанк; забился — заводится следующий и линкуется типом связи
  **Successor** (`System.LinkTypes.Dependency-Forward`, «предыдущий → следующий
  чанк»). В Boards видно цепочку и через колонку зависимостей.
- Всё автоматически: `AdoClient.attach_video(channel, kind, video)` сам выбирает
  чанк, доливает следующий и дедупит видео по ВСЕМ чанкам канала. Дедуп
  `channel_all_child_video_ids`. Первый чанк — без суффикса `#1` для читаемости.
- Это же снимает display-лимит бэклога (10k): каждый чанк — отдельный Epic.

Смотреть «базу»: Boards → **Backlogs** → уровень Epics → разворачиваешь чанк
канала и видишь его видео со статусами; следующий чанк — по связи Successor. Rollup-колонка (настройка Column options →
Rollup → Progress by Work Items) даёт процент обработанности канала.

Полезные query (Boards → Queries → New query):
- очередь на парсинг: `Work Item Type = Task AND State = New`
- ждут Клода: `State = ReadyForFilter`; на эмбеддинг: `State = ReadyForEmbeding`
- брак: `State = Removed AND Tags Contains failed`
- видео одного канала: **Tree of work items**, Epic title `Contains ch:UCxxxx`
- прогресс: **Work items and direct links**, linked `State = Closed`

## Когда захочется красоты (не сейчас)

Organization settings → Process → создать **inherited process** от Basic →
новый тип work item **Video** с полями:
- `Custom.VideoId` (string, уникальный) — вместо маркера в заголовке
- `Custom.Channel`, `Custom.Lang`, `Custom.DurationSec`
- `Custom.PipelineState` (picklist) — вместо тегов, + правила переходов

Затем в `.env` поменять `ADO_WORKITEM_TYPE=Video`, а в `pipeline/ado.py`
дедуп/состояния переключить на эти поля (места помечены комментариями).

Маппинг «код ↔ твой шаблон», если модифицируешь process template под себя:
- тип work item — env `ADO_WORKITEM_TYPE` (код не привязан к Issue);
- состояние конвейера — сейчас теги `state:*` (`ADO_STATE_PREFIX` в config.py);
  перейдёшь на кастомное поле/колонки доски — меняется только `set_state`/
  `query_by_state` в ado.py (две функции);
- идентификатор видео — маркер `[vid:...]` в Title; с кастомным полем
  `Custom.VideoId` замени `find_video_item`/`video_id_from_title`;
- атомарный клейм для парных аккаунтов (`claim`) работает на System.Rev —
  от шаблона не зависит, трогать не надо.

## Weekly discovery pipeline

1. Репо запушить в Azure Repos (или GitHub — ADO умеет оба).
2. Pipelines → New pipeline → Existing YAML → `ado-pipelines/discovery-weekly.yml`.
3. Library → Variable group **automech-secrets**:
   - `YOUTUBE_API_KEY` (Google Cloud Console → YouTube Data API v3 → ключ)
   - `ADO_PAT_SECRET` (тот же PAT)
   - пометить оба как secret.
4. Первый прогон — вручную (Run pipeline), потом по cron (пн 04:00 UTC).

## Service hook (опционально, вместо почасового поллинга CircleCI)

Project settings → Service hooks → Web Hooks → «Work item created» →
POST на CircleCI API v2 (`https://circleci.com/api/v2/project/{slug}/pipeline`,
заголовок `Circle-Token`). На MVP хватает расписания — hooks добавишь, когда
захочется реактивности.
