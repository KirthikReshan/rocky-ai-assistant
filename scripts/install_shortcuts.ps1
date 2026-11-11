$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$manifest = Join-Path $project 'current-release.txt'
$executable = [IO.File]::ReadAllText($manifest).Trim()
if (-not $executable.StartsWith($project + '\', [StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-Path -LiteralPath $executable)) { throw 'Build Rocky before installing shortcuts.' }
$shell = New-Object -ComObject WScript.Shell
$launcher = Join-Path $project 'Rocky.vbs'
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
foreach ($location in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    foreach ($app in @(@('Rocky Assistant', ''), @('Rocky Calendar', ' /calendar'))) {
        $shortcutPath = Join-Path $location ($app[0] + '.lnk')
        $shortcut = $shell.CreateShortcut($shortcutPath)
        if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.Arguments -notlike ('*' + $launcher + '*') -and
            $shortcut.Description -ne 'Rocky managed launcher') {
            throw "An unrelated shortcut already exists at $shortcutPath."
        }
        $shortcut.TargetPath = $wscript
        $shortcut.Arguments = '"' + $launcher + '"' + $app[1]
        $shortcut.WorkingDirectory = $project
        $shortcut.IconLocation = $executable + ',0'
        $shortcut.Description = 'Rocky managed launcher'
        $shortcut.Save()
    }
}
$startup = [Environment]::GetFolderPath('Startup')
Get-ChildItem -LiteralPath $startup -Filter 'Rocky Calendar *.lnk' | ForEach-Object {
    $shortcut = $shell.CreateShortcut($_.FullName)
    if ($shortcut.TargetPath.StartsWith($project + '\', [StringComparison]::OrdinalIgnoreCase)) {
        $shortcut.Arguments = $shortcut.Arguments -replace '^-m rocky.calendar_service', '--calendar'
        $shortcut.TargetPath = $executable
        $shortcut.WorkingDirectory = Split-Path -Parent $executable
        $shortcut.Save()
    }
}
