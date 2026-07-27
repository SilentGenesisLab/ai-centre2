$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONPATH = Join-Path $Root "src"
$env:OCR_CONFIG = Join-Path $Root "config\ocr.tor25.gpu.json"
& "D:\Anaconda3\envs\ocrpaddle\python.exe" -m ocr_service.cli
