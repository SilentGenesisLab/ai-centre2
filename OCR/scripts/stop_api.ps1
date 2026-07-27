$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "runtime\ocr-api.pid"
if (!(Test-Path $PidFile)) {
  Write-Host "OCR API pid file not found"
  exit 0
}
$ServicePid = [int](Get-Content $PidFile -Raw)
$Process = Get-Process -Id $ServicePid -ErrorAction SilentlyContinue
if ($Process) {
  Stop-Process -Id $ServicePid -Force
  Write-Host "OCR API stopped: $ServicePid"
} else {
  Write-Host "OCR API process not running: $ServicePid"
}
Remove-Item $PidFile -Force
