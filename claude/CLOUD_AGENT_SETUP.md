# Ночной Claude-агент дистилляции — отдельный репо + scheduled task

Роль: пока CircleCI собирает сырьё в ADO-тикеты (state:subs), **твой Claude в
облаке ночью** берёт их, сам (как сильная модель — без внешних ключей)
дистиллирует в структурный кейс и пишет результат **назад в тикет**. Это и есть
«мозг», о котором ты говорил.

## Почему отдельный репо
claude.ai scheduled task подключается к ОДНОМУ GitHub-репо под твоим ОСНОВНЫМ
Claude-аккаунтом (не под burner-аккаунтами acc1/acc2 скрапера). Чтобы не мешать
скрапер-флоу (CircleCI) с флоу дистилляции — заводим отдельный приватный репо с
тем же кодом (нужен только пакет `pipeline/` + `requirements.txt`).

## 1. Создать отдельный репо (под твоим основным GitHub)
Вариант проще — зеркало текущего кода:
```
# из C:\aa\automechanic (git уже инициализирован)
git remote add distill https://github.com/<ТВОЙ-ОСНОВНОЙ-GH>/automech-distill.git
git push distill main
```
(репо `automech-distill` создай приватным в вебе; secrets в git не идут — `.env`,
`accounts.json` в .gitignore). Если хочешь — скажи имя, и я запушу сам под нужным
токеном.

## 2. claude.ai → Scheduled task (routine)
1. Подключи репо `automech-distill` к сессии агента.
2. В **env агента** задай секретами: `ADO_ORG=gpsgroupagent12`,
   `ADO_PROJECT=AutoMechanic`, `ADO_PAT=<тот же PAT>`. (Опц. `S3_*` — если позже
   заведёшь архив; без него кейс всё равно ложится В ТЕЛО ТИКЕТА.)
3. Расписание: ночью, когда свободно (напр. cron `0 2 * * *`), 1 прогон = 1 батч;
   поставь несколько прогонов подряд, если очередь большая.
4. **Промпт агента** = содержимое `claude/DISTILL_AGENT.md` (скопируй целиком).

## 3. Что агент делает каждый прогон (уже готово в коде)
- `python -m pipeline.tools next-subs --batch 5` — заклеймит батч state:subs и
  вернёт JSON: для видео — транскрипт, **для форумов — текст треда из тела тикета**
  (поле `source_type`).
- Claude сам строит `RepairCase` по схеме (`claude/DISTILL_AGENT.md` +
  `pipeline/case_schema.py`): симптомы, звуки, замеры, DTC, pitfalls со scope,
  правила «если→то», applicability, скепсис-доктрина.
- `python -m pipeline.tools save-case <wi_id> case.json` — валидирует, **пишет
  кейс назад в тело тикета** и переводит в state:distilled (или offtopic).
- Проверено: механика round-trip работает (тикет #15). Разница только в модели —
  теперь это Claude, а не заглушка Pollinations.

Итог: CircleCI (сбор) → ADO (тикеты) → ночной Claude (дистилляция) → ADO (кейс в
тикете). R2/Cloudflare для этого не нужен.
