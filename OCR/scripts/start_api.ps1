$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root "runtime"
$Logs = Join-Path $Runtime "logs"
New-Item -ItemType Directory -Force $Runtime, $Logs | Out-Null
$PidFile = Join-Path $Runtime "ocr-api.pid"
if (Test-Path $PidFile) {
  $ExistingPid = [int](Get-Content $PidFile -Raw)
  $Existing = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
  if ($Existing) {
    Write-Host "OCR API already running: $ExistingPid"
    exit 0
  }
}
$Env:PYTHONPATH = Join-Path $Root "src"
$Env:OCR_CONFIG = Join-Path $Root "config\ocr.local.json"
$Out = Join-Path $Logs "ocr-api.out.log"
$Err = Join-Path $Logs "ocr-api.err.log"
$Process = Start-Process -FilePath "python" `
  -ArgumentList "-m", "ocr_service.cli" `
  -WorkingDirectory $Root `
  -RedirectStandardOutput $Out `
  -RedirectStandardError $Err `
  -WindowStyle Hidden `
  -PassThru
Set-Content -Path $PidFile -Value $Process.Id -Encoding ASCII
Write-Host "OCR API started: $($Process.Id)"
