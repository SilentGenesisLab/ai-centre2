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
$PythonPath = Join-Path $Root "src"
$ConfigPath = Join-Path $Root "config\ocr.tor25.gpu.json"
$Out = Join-Path $Logs "ocr-api.out.log"
$Err = Join-Path $Logs "ocr-api.err.log"
$Command = "`$env:PYTHONPATH='$PythonPath'; `$env:OCR_CONFIG='$ConfigPath'; & 'D:\Anaconda3\envs\ocrpaddle\python.exe' -m ocr_service.cli"
$Process = Start-Process -FilePath "powershell" `
  -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $Command `
  -WorkingDirectory $Root `
  -RedirectStandardOutput $Out `
  -RedirectStandardError $Err `
  -WindowStyle Hidden `
  -PassThru
Set-Content -Path $PidFile -Value $Process.Id -Encoding ASCII
Write-Host "OCR API ocrpaddle GPU started: $($Process.Id)"
