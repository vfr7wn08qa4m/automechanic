@echo off
REM AutoMech: локальный краул форумов за JS-челленджем Cloudflare (зона D, vwvortex).
REM Запускается планировщиком Windows каждые 4 часа (смещён на 1ч от drive2).
REM Таймбокс 30 мин: тянет треды -> тикеты ADO (state:subs), фронтир помнит место,
REM следующий запуск продолжает. Логи -> data\forumsjs_crawl.log.
cd /d C:\aa\automechanic
"C:\Users\Maksym.Karyonov\AppData\Local\Programs\Python\Python313\python.exe" scripts\local_crawl_forums_js.py --minutes 30 >> data\forumsjs_crawl.log 2>&1
