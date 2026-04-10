Dim python, script
python = "D:\Code\yolo\Claude_Code\LocalHelpBot\venv\Scripts\python.exe"
script = "D:\Code\yolo\Claude_Code\LocalHelpBot\core\proxy.py"

Set WshShell = CreateObject("WScript.Shell")
WshShell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
