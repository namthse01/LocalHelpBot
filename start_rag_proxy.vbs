Dim python, script
python = "d:\Nam_Work\yolo\rag\venv\Scripts\python.exe"
script = "d:\Nam_Work\yolo\rag\rag_proxy.py"

Set WshShell = CreateObject("WScript.Shell")
WshShell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
