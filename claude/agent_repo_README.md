# claudeautomation — ночной Claude-агент дистилляции AutoMech

Отдельный репо для scheduled Claude-агента (claude.ai routines). CircleCI и
домашний сборщик наполняют Azure DevOps сырьём в теле тикетов; этот агент ночью
превращает сырьё в структурированные кейсы ремонта и пишет их назад в тикеты.

Инструкция агенту — в `CLAUDE.md` (читается автоматически). Код —
`pipeline/tools.py` (`next-subs` / `save-case` / `fail`), работает по `ADO_*` из env.

## Настройка scheduled task (claude.ai/code/routines)

1. **claude.ai/code/routines → New routine.** Имя, напр. `AutoMech Nightly Distill`.
2. **Repositories:** выбери этот приватный репо `x2n873ju8wd21w4/claudeautomation`.
   Если попросит — установи **Claude GitHub App** на репо (обязателен для клона +
   расписания). Каждый прогон = свежий клон default-ветки.
3. **Environment → settings (иконка шестерёнки) → Environment variables** (формат
   `.env`, по строке):
   ```
   ADO_ORG=gpsgroupagent12
   ADO_PROJECT=AutoMechanic
   ADO_PAT=<PAT scope Work Items R/W — тот же, что в accounts.json.azure.pat>
   ```
   ⚠️ У claude.ai пока НЕТ отдельного секрет-хранилища: переменные видны тем, кто
   может редактировать это окружение. Держи отдельное окружение под эту рутину и
   ротируй PAT. (Код секреты в вывод не печатает.)
4. **Prompt рутины** (что делать каждый прогон):
   ```
   Выполни один прогон дистилляции AutoMech по инструкции из CLAUDE.md:
   pip install -r requirements.txt, затем python -m pipeline.tools next-subs
   --batch 5, построй RepairCase по схеме для каждого элемента и сохрани через
   save-case. В конце — сводка distilled/offtopic/failed. Если очередь пуста —
   так и напиши.
   ```
   Хочешь больше за ночь — увеличь `--batch` или добавь несколько прогонов подряд.
5. **Schedule:** daily, ночью (когда свободно). **Create.**

## Проверка вручную (локально, до расписания)
```
pip install -r requirements.txt
export ADO_ORG=... ADO_PROJECT=... ADO_PAT=...   # Windows: set ...
python -m pipeline.tools next-subs --batch 2      # покажет сырьё JSON'ом
```

## Что откуда
- Сырьё в тикетах готовит скрапер-репо `autoscrapper` (CircleCI: форумы + реле;
  YouTube-субтитры — из дома). Этот репо только ДИСТИЛЛИРУЕТ.
- `save-case` пишет кейс в тело тикета И (если задан `S3_*`) в R2. R2 не обязателен.
