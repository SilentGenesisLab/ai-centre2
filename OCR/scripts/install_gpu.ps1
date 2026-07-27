$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Write-Host "Install PaddlePaddle GPU wheel that matches your CUDA runtime."
Write-Host "For production on the 5090 Linux server we use PaddlePaddle cu129 in the ailab environment."
Write-Host "On Windows, if the GPU wheel is unavailable, use install_cpu.ps1 or deploy this service in WSL/Linux."
