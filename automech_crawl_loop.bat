@echo off
REM AutoMech: НЕПРЕРЫВНЫЙ локальный краул форумов (round-robin, домашний IP).
REM Один процесс бесконечно гоняет три форума по кругу — всегда активен ровно один
REM краулер, каждый форум ревизитится примерно раз в час (мягче, чем долбить один
REM нон-стоп; естественная пауза = время двух других краулеров).
REM   drive2   (DDoS-Guard)      -> тикеты ADO state:subs, курсор помнит место
REM   forumsjs (vwvortex, CF/JS) -> Playwright, фронтир помнит место
REM   autohome (кит. Q&A)        -> ADO-дедуп = резюме
REM Запускать СКРЫТО через run_hidden.vbs (планировщик, триггер At logon), чтобы не
REM было чёрного окна и процесс доживал (Ctrl+C не прилетает). Локи (data\*.lock,
REM STALE_MIN=45) не дают двойного запуска, если задача случайно стартанёт дважды.
cd /d C:\aa\automechanic
set PY="C:\Users\Maksym.Karyonov\AppData\Local\Programs\Python\Python313\python.exe"
:loop
%PY% scripts\local_crawl_drive2.py --minutes 20     >> data\drive2_crawl.log 2>&1
%PY% scripts\local_crawl_forums_js.py --minutes 20  >> data\forumsjs_crawl.log 2>&1
%PY% scripts\local_crawl_autohome.py --minutes 20   >> data\autohome_crawl.log 2>&1
timeout /t 20 /nobreak >nul
goto loop
