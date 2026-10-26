Option Explicit

Dim sh, cmd, rc, url, proc, started, output, re, m
Set sh = CreateObject("WScript.Shell")
url = "http://127.0.0.1:8770/"
sh.Popup "Starting Feature Portal...", 2, "Feature Portal", 64
cmd = "wsl.exe -d Ubuntu-20.04 --exec /bin/bash /home/hgw/workspace/github/active-fo-porting/feature-portal/scripts/feature-portal/start-service.sh 8770"
Set proc = sh.Exec(cmd)
started = Timer
Do While proc.Status = 0
  WScript.Sleep 100
  If Timer - started > 5 Then
    rc = 124
    MsgBox "WSL startup timed out. See feature-portal-launch.log.", 16, "Feature Portal"
    WScript.Quit 124
  End If
Loop
If proc.Status <> 0 And rc = 0 Then rc = proc.ExitCode
If rc = 0 Then
  output = proc.StdOut.ReadAll
  Set re = New RegExp
  re.Pattern = "ip=([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)"
  If re.Test(output) Then
    Set m = re.Execute(output)(0)
    url = "http://" & m.SubMatches(0) & ":8770/"
  End If
  ' Use the Windows shell first; keep rundll32 as a fallback for machines
  ' where ShellExecute is not available through WScript.
  On Error Resume Next
  sh.Run "cmd.exe /c start """" " & url, 0, False
  If Err.Number <> 0 Then
    Err.Clear
    sh.Run "explorer.exe " & url, 0, False
  End If
  If Err.Number <> 0 Then
    Err.Clear
    sh.Run "rundll32.exe url.dll,FileProtocolHandler " & url, 0, False
  End If
  On Error GoTo 0
Else
  MsgBox "Service startup failed, exit code: " & rc & vbCrLf & "See feature-portal-launch.log.", 16, "Feature Portal"
End If
