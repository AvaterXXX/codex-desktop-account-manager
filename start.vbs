' Launch GUI without any console window
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
mainPy = dir & "\main.py"

' Try pythonw.exe first
On Error Resume Next
sh.CurrentDirectory = dir
sh.Run "pythonw.exe """ & mainPy & """", 0, False
If Err.Number = 0 Then WScript.Quit 0
Err.Clear
sh.Run "py.exe -3w """ & mainPy & """", 0, False
If Err.Number = 0 Then WScript.Quit 0
Err.Clear
' Fallback (may flash console briefly)
sh.Run "python.exe """ & mainPy & """", 0, False
