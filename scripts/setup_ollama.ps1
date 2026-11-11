$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $projectRoot 'models\OllamaSetup-0.40.13.exe'
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $ollamaExe)) {
    Write-Output 'Downloading/resuming official Ollama installer (1.56 GB).'
    & curl.exe -L --fail --retry 3 --connect-timeout 20 --max-time 7200 -C - -o $installer 'https://github.com/ollama/ollama/releases/download/v0.40.13/OllamaSetup.exe'
    if ($LASTEXITCODE -ne 0) { throw 'Download incomplete. Run this script again to resume.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Ollama') {
        throw 'Installer signature validation failed; installation stopped.'
    }
    Write-Output 'Installing signed Ollama package for the current Windows user.'
    $installation = Start-Process -FilePath $installer -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-' -WindowStyle Hidden -Wait -PassThru
    if ($installation.ExitCode -ne 0) { throw "Installer exited with $($installation.ExitCode)" }
}

try {
    $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 3
} catch {
    Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
            $ready = $true
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Ollama did not start. Check its Windows server log.' }
}

Write-Output 'Downloading Qwen3 4B (approximately 2.5 GB).'
& $ollamaExe pull qwen3:4b
if ($LASTEXITCODE -ne 0) { throw 'Model download failed. Run this script again to retry.' }
Set-Location -LiteralPath $projectRoot
& $pythonExe -c "from rocky.brain import OllamaBrain; print(OllamaBrain().chat('Introduce yourself in one sentence.'))"
if ($LASTEXITCODE -ne 0) { throw 'Local chat smoke test failed.' }
& $ollamaExe ps
Write-Output 'Ollama setup and Rocky local chat smoke test completed.'
