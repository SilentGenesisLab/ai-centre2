$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONPATH = Join-Path $Root "src"
$env:OCR_CONFIG = Join-Path $Root "config\ocr.local.json"
python -m ocr_service.cli
