$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install paddlepaddle
