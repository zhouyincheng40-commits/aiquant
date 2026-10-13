' ===========================================================================
'  start-supervisor.vbs  --  launch relay-supervisor.ps1 FULLY DETACHED.
'
'  Two lessons are baked into this file:
'   1) Anything started from a console inherits that console and dies with it.
'      WScript.Shell.Run (style 0, wait=False) detaches completely, so the
'      supervisor survives the DSH session and keeps restarting the relay.
'   2) PowerShell 5.1 reads BOM-less files as ANSI, so a CJK literal path in a
'      .ps1 silently turns into a MOJIBAKE directory. This file stays ASCII and
'      the target script resolves its own paths from $PSScriptRoot, so no real
'      path ever travels through script-text decoding.
'
'  Usage:  wscript.exe <this dir>\start-supervisor.vbs
'  Stop:   Stop-Process -Id (Get-Content <this dir>\logs\supervisor.pid)
'  Watch:  Get-Content <this dir>\logs\supervisor.log -Tail 20 -Encoding UTF8
' ===========================================================================

Option Explicit

Dim fso, shell, here, supscript, logdir, pidfile, txt, oldPid
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here      = fso.GetParentFolderName(WScript.ScriptFullName)
supscript = here & "\relay-supervisor.ps1"
logdir    = here & "\logs"
pidfile   = logdir & "\supervisor.pid"

If Not fso.FileExists(supscript) Then
    WScript.Echo "[start-supervisor] not found: " & supscript
    WScript.Quit 1
End If

If Not fso.FolderExists(logdir) Then fso.CreateFolder logdir

' single-instance guard: is the recorded pid still alive?
If fso.FileExists(pidfile) Then
    On Error Resume Next
    txt = fso.OpenTextFile(pidfile, 1).ReadAll()
    oldPid = Trim(txt)
    If Len(oldPid) > 0 Then
        Dim wmi, procs
        Set wmi = GetObject("winmgmts:\\.\root\cimv2")
        Set procs = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE ProcessId=" & oldPid)
        If procs.Count > 0 Then
            WScript.Echo "[start-supervisor] already running (pid " & oldPid & ") - nothing to do."
            WScript.Quit 0
        End If
    End If
    On Error GoTo 0
End If

shell.CurrentDirectory = here
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & supscript & """", 0, False

WScript.Echo "[start-supervisor] launched from " & here
WScript.Echo "[start-supervisor] log: " & logdir & "\supervisor.log"
WScript.Echo "[start-supervisor] pid: " & pidfile
