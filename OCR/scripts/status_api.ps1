$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "runtime\ocr-api.pid"
if (Test-Path $PidFile) {
  $ServicePid = [int](Get-Content $PidFile -Raw)
  $Process = Get-Process -Id $ServicePid -ErrorAction SilentlyContinue
  if ($Process) {
    Write-Host "OCR API running: $ServicePid"
  } else {
    Write-Host "OCR API pid exists but process is not running: $ServicePid"
  }
} else {
  Write-Host "OCR API not running"
}
try {
  Invoke-RestMethod -Uri "http://127.0.0.1:8096/health" -TimeoutSec 3 | ConvertTo-Json -Depth 8
} catch {
  Write-Host "health check failed: $($_.Exception.Message)"
}
