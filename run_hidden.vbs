' AutoMech: тихий запуск .bat БЕЗ чёрного окна (window mode 0).
' Аргумент 1 = полный путь к .bat. Запускается планировщиком через wscript.exe.
' Так scheduled-краул не показывает консоль -> юзер не закроет окно -> процесс
' не ловит Ctrl+C (0xC000013A) и доживает свой таймбокс. Вывод идёт в лог (в .bat).
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count > 0 Then
    sh.Run """" & WScript.Arguments(0) & """", 0, False
End If
