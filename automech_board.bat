@echo off
rem AutoMech — живой борд конвейера (копия в репо; на рабочем столе — ярлык
rem "AutoMech board.bat"). Двойной клик = открыть актуальный борд.
cd /d "%~dp0"

rem погасить прошлый сервер на 8788
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8788" ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>&1

start "AutoMech board server" python scripts\board_server.py --port 8788 --refresh 90
timeout /t 3 /nobreak >nul
start "" http://localhost:8788
