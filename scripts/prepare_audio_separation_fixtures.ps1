param(
    [string]$InputRoot = "I:\self_tool\ai-centre2\data\videos",
    [string]$OutputRoot = "I:\self_tool\ai-centre2\docs\reports\video-audio-separation-20260820"
)

$ErrorActionPreference = "Stop"
$audioRoot = Join-Path $OutputRoot "source-audio-v2"
New-Item -ItemType Directory -Force -Path $audioRoot | Out-Null

$rows = @()
$fixtureIndex = 0
foreach ($file in Get-ChildItem -LiteralPath $InputRoot -File -Recurse | Sort-Object FullName) {
    $fixtureIndex++
    $group = $file.Directory.Name
    $fixtureId = "fixture-{0:D3}" -f $fixtureIndex
    $wavPath = Join-Path $audioRoot "$fixtureId.wav"

    & ffmpeg -hide_banner -loglevel error -y -i $file.FullName -vn -ac 2 -ar 48000 -c:a pcm_s16le $wavPath
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed: $($file.FullName)" }

    $probe = & ffprobe -v error -show_entries "format=duration,size,bit_rate:stream=codec_name,sample_rate,channels" -of json -- $wavPath | ConvertFrom-Json
    $rows += [ordered]@{
        fixture_id = $fixtureId
        source_group = $group
        source_file = $file.FullName
        source_name = $file.Name
        audio_file = $wavPath
        duration_seconds = [math]::Round([double]$probe.format.duration, 3)
        sample_rate = [int]$probe.streams[0].sample_rate
        channels = [int]$probe.streams[0].channels
        audio_bytes = [int64]$probe.format.size
        expected_language = "auto"
    }
}

$manifestPath = Join-Path $OutputRoot "fixtures.json"
$manifestJson = $rows | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($manifestPath, $manifestJson, [Text.UTF8Encoding]::new($false))
Write-Output "Prepared $($rows.Count) fixtures"
Write-Output $manifestPath
