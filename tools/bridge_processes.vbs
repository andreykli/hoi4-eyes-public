Option Explicit
Dim svc, proc, cmd, exe
Set svc = GetObject("winmgmts:\\.\root\cimv2")
For Each proc In svc.ExecQuery("SELECT ProcessId, ParentProcessId, CreationDate, ExecutablePath, CommandLine FROM Win32_Process WHERE Name='python.exe' OR Name='pythonw.exe'")
  cmd = ""
  exe = ""
  If Not IsNull(proc.CommandLine) Then cmd = Replace(Replace(proc.CommandLine, vbTab, " "), vbCrLf, " ")
  If Not IsNull(proc.ExecutablePath) Then exe = proc.ExecutablePath
  WScript.Echo proc.ProcessId & vbTab & proc.ParentProcessId & vbTab & proc.CreationDate & vbTab & exe & vbTab & cmd
Next
