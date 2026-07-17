@echo off
REM AutoMech: борд-сервер БЛОКИРУЮЩЕ (без start), чтобы run_hidden.vbs держал его
REM скрыто. Пульт [1] гасит старый :8788 и поднимает этот через vbs -> окна нет.
cd /d C:\aa\automechanic
"C:\Users\Maksym.Karyonov\AppData\Local\Programs\Python\Python313\python.exe" scripts\board_server.py --port 8788 --refresh 90
