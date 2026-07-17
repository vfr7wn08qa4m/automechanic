@echo off
REM AutoMech: локальный краул autohome (домашний IP, китайский Q&A за анти-ботом).
REM Запускается планировщиком Windows каждые 4 часа (смещён на 2ч от drive2).
REM Таймбокс 30 мин: тянет порцию тредов -> тикеты ADO (state:subs), ADO-дедуп =
REM резюме (следующий запуск продолжает). Логи -> data\autohome_crawl.log.
cd /d C:\aa\automechanic
"C:\Users\Maksym.Karyonov\AppData\Local\Programs\Python\Python313\python.exe" scripts\local_crawl_autohome.py --minutes 30 >> data\autohome_crawl.log 2>&1
