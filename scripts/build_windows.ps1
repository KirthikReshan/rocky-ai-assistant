param([string]$DistPath = '', [switch]$SkipShortcuts)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not $DistPath) {
    $version = & $python -c "import runpy,sys; print(runpy.run_path(sys.argv[1])['VERSION'])" (Join-Path $project 'rocky\version.py')
    if ($LASTEXITCODE -ne 0) { throw 'Rocky version could not be read.' }
    $DistPath = 'releases/' + $version.Trim() + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
}
$releaseRoot = [IO.Path]::GetFullPath((Join-Path $project $DistPath))
if ([IO.Path]::IsPathRooted($DistPath)) { $releaseRoot = [IO.Path]::GetFullPath($DistPath) }
if (-not $releaseRoot.StartsWith($project + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Build output must be inside the Rocky project.'
}
$lockFolder = Join-Path $project 'build'
New-Item -ItemType Directory -Path $lockFolder -Force | Out-Null
try {
    $buildLock = [IO.File]::Open((Join-Path $lockFolder 'windows-build.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    throw 'Another Rocky Windows build is running.'
}
Push-Location -LiteralPath $project
$previousPyInstallerCache = $env:PYINSTALLER_CONFIG_DIR
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $lockFolder 'pyinstaller-cache'
    $options = @('--clean', '--noconfirm', '--windowed', '--onedir', '--name', 'Rocky', '--paths', '.', '--specpath', 'build',
                 '--distpath', $releaseRoot)
    $options += @('--additional-hooks-dir', (Join-Path $PSScriptRoot 'pyinstaller_hooks'))
    foreach ($package in @('vosk', 'faster_whisper', 'ctranslate2', 'nvidia.cublas', 'nvidia.cuda_nvrtc')) {
        $options += @('--collect-all', $package)
    }
    foreach ($package in @('piper', 'dateparser', 'tzdata', 'pypdf', 'docx', 'rapidocr_onnxruntime')) {
        $options += @('--collect-data', $package)
    }
    $options += @('--hidden-import', 'win32timezone',
                  '--add-data', "${project}/models/vosk-model-small-en-us-0.15:models/vosk-model-small-en-us-0.15",
                  '--add-data', "${project}/models/voices:models/voices")
    foreach ($file in @('config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt')) {
        $options += @('--add-data', "${project}/models/whisper-small.en/${file}:models/whisper-small.en")
    }
    & $python -m PyInstaller @options scripts/windows_app.py
    if ($LASTEXITCODE -ne 0) { throw 'Rocky build failed.' }
    $executable = Join-Path $releaseRoot 'Rocky\Rocky.exe'
    if (-not $executable.StartsWith($project + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $executable)) { throw 'Release must be inside the Rocky project.' }
    $manifest = Join-Path $project 'current-release.txt'
    [IO.File]::WriteAllText($manifest + '.new', $executable, [Text.Encoding]::Unicode)
    # .NET Framework / Windows PowerShell 5.1 lacks File.Move's overwrite overload.
    if ([IO.File]::Exists($manifest)) {
        [IO.File]::Replace($manifest + '.new', $manifest, [NullString]::Value)
    } else {
        [IO.File]::Move($manifest + '.new', $manifest)
    }
    if (-not $SkipShortcuts) {
        & (Join-Path $PSScriptRoot 'install_shortcuts.ps1')
    }
} finally {
    $env:PYINSTALLER_CONFIG_DIR = $previousPyInstallerCache
    Pop-Location
    $buildLock.Dispose()
}
