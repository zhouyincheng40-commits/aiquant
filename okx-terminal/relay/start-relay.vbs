' ===========================================================================
'  start-relay.vbs  --  launch the OKX relay FULLY DETACHED from any console.
'
'  Why a VBScript:
'    Starting the relay through cmd.exe / powershell means the child inherits
'    the parent's console (and its stdin). When that shell exits, Windows
'    delivers Ctrl+C to the whole console process group and the relay dies
'    with "^C" in stdout.log. That happened twice.
'    WScript.Shell.Run with window style 0 detaches it from any console,
'    so the relay keeps running after the launching shell (and this DSH
'    session) is gone.
'
'  Usage:  wscript.exe D:\<path>\start-relay.vbs
'  Stop:   get the pid from relay\logs\relay.pid, then  Stop-Process -Id <pid>
' ===========================================================================

Option Explicit

Dim fso, shell, here, py, script, workdir, cmdline
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)

py     = "C:\Users\33096\.conda\envs\aiquant\python.exe"
script = here & "\relay.py"

If Not fso.FileExists(py) Then
    WScript.Echo "[start-relay] python not found: " & py
    WScript.Quit 1
End If
If Not fso.FileExists(script) Then
    WScript.Echo "[start-relay] relay.py not found: " & script
    WScript.Quit 1
End If

shell.CurrentDirectory = here

' 0 = hidden window, False = do not wait
cmdline = "cmd /c set PYTHONUTF8=1&& set PYTHONIOENCODING=utf-8&& """ & py & """ """ & script & """"
shell.Run cmdline, 0, False

WScript.Echo "[start-relay] launched detached from " & here
WScript.Echo "[start-relay] url: http://127.0.0.1:8910/  (ws /ws, sse /sse, debug /debug)"
WScript.Echo "[start-relay] log: " & here & "\logs\relay.log"
