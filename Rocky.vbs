Option Explicit
Dim files, shell, folder, manifest, app, mode
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = files.GetParentFolderName(WScript.ScriptFullName)
manifest = folder & "\current-release.txt"
If Not files.FileExists(manifest) Then
    MsgBox "Rocky needs a Windows build. Run scripts\build_windows.ps1 first.", 48, "Rocky"
    WScript.Quit 1
End If
Dim reader
Set reader = files.OpenTextFile(manifest, 1, False, -1)
app = Trim(reader.ReadAll)
reader.Close
app = files.GetAbsolutePathName(app)
If LCase(Left(app, Len(folder) + 1)) <> LCase(folder & "\") Or LCase(files.GetFileName(app)) <> "rocky.exe" Then
    MsgBox "The Rocky release path is invalid. Rebuild Rocky.", 48, "Rocky"
    WScript.Quit 1
End If
If Not files.FileExists(app) Then
    MsgBox "The Rocky build could not be found. Rebuild Rocky.", 48, "Rocky"
    WScript.Quit 1
End If
mode = ""
If WScript.Arguments.Count > 0 Then
    If LCase(WScript.Arguments(0)) = "/calendar" Then mode = " --calendar-page"
End If
shell.CurrentDirectory = files.GetParentFolderName(app)
shell.Run Chr(34) & app & Chr(34) & mode, 1, False
