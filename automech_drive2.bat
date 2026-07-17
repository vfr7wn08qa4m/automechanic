@echo off
REM AutoMech: локальный краул drive2 (домашний IP, drive2 за DDoS-Guard).
REM Запускается планировщиком Windows при входе + периодически. Таймбокс 30 мин:
REM тянет порцию бортжурналов -> тикеты ADO (state:subs), курсор помнит место,
REM следующий запуск продолжает. Логи -> data\drive2_crawl.log.
cd /d C:\aa\automechanic
"C:\Users\Maksym.Karyonov\AppData\Local\Programs\Python\Python313\python.exe" scripts\local_crawl_drive2.py --minutes 30 >> data\drive2_crawl.log 2>&1
