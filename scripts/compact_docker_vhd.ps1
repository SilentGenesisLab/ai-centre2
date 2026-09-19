$ErrorActionPreference = "Stop"

$vhdPath = "C:\Users\Admin\AppData\Local\Docker\wsl\disk\docker_data.vhdx"
$markerPath = "I:\self_tool\ai-centre2\runtime_validation\docker-vhd-compact-result.txt"
$diskpartPath = Join-Path $env:TEMP "ai-centre2-docker-compact.txt"

if (-not (Test-Path -LiteralPath $vhdPath)) {
    throw "Docker VHDX not found: $vhdPath"
}

& "C:\Program Files\Docker\Docker\resources\bin\docker.exe" desktop stop --force --timeout 60
& "$env:SystemRoot\System32\wsl.exe" --shutdown

@"
select vdisk file="$vhdPath"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@ | Set-Content -LiteralPath $diskpartPath -Encoding ascii

try {
    $output = & "$env:SystemRoot\System32\diskpart.exe" /s $diskpartPath 2>&1
    $exitCode = $LASTEXITCODE
    $size = (Get-Item -LiteralPath $vhdPath).Length
    @(
        "exit_code=$exitCode"
        "size_bytes=$size"
        "finished_at=$([DateTimeOffset]::Now.ToString('o'))"
        $output
    ) | Set-Content -LiteralPath $markerPath -Encoding utf8
    if ($exitCode -ne 0) { throw "diskpart compact failed with exit code $exitCode" }
}
finally {
    Remove-Item -LiteralPath $diskpartPath -Force -ErrorAction SilentlyContinue
}
