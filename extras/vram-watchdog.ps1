param(
    [string]$Model = "",
    [int]$IdleSeconds = 600,
    [int]$GpuThresholdMB = 4096,
    [int]$PollSeconds = 15
)

$ErrorActionPreference = "SilentlyContinue"
if (-not $Model) { $Model = $env:VISION_TEXT_MODEL }
if (-not $Model) { $Model = "haervwe/GLM-4.6V-Flash-9B" }
$ollamaBase = $env:OLLAMA_HOST
if (-not $ollamaBase) { $ollamaBase = "http://localhost:11434" }
$markerDir = Join-Path $env:TEMP "codex-vision"
New-Item -ItemType Directory -Force -Path $markerDir | Out-Null
$marker = Join-Path $markerDir ".last-use"
$pidFile = Join-Path $markerDir ".watchdog.pid"
Set-Content -Path $pidFile -Value $PID -Encoding ASCII

function Stop-Model {
    $targets = @()
    try {
        $ps = Invoke-RestMethod -Uri "$ollamaBase/api/ps" -Method Get -TimeoutSec 10
        foreach ($m in $ps.models) {
            if ($m.name) { $targets += $m.name }
        }
    } catch {}
    if ($targets.Count -eq 0) {
        $targets = @($Model)
    }
    foreach ($name in $targets) {
        try {
            $body = @{ model = $name; prompt = ""; keep_alive = 0 } | ConvertTo-Json
            Invoke-RestMethod -Uri "$ollamaBase/api/generate" -Method Post `
                -ContentType "application/json" -Body $body -TimeoutSec 15 | Out-Null
        } catch {}
    }
}

function Test-HighVramOtherProcess {
    $nvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $nvidia) { return $false }
    $apps = & nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>$null
    foreach ($line in $apps) {
        $parts = $line -split ","
        if ($parts.Count -lt 2) { continue }
        $procPid = $parts[0].Trim()
        $mem = 0
        [int]::TryParse($parts[1].Trim(), [ref]$mem) | Out-Null
        if ($mem -le $GpuThresholdMB) { continue }
        $procName = (Get-Process -Id $procPid -ErrorAction SilentlyContinue).ProcessName
        if ($procName -and $procName -notmatch "ollama|nvidia") {
            return $true
        }
    }
    return $false
}

while ($true) {
    # 1) unload when Codex is closed, then exit
    $codexRunning = @(Get-Process -Name "codex*" -ErrorAction SilentlyContinue).Count -gt 0
    if (-not $codexRunning) {
        Stop-Model
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        break
    }

    # 2) unload when another process starts using a lot of VRAM
    if (Test-HighVramOtherProcess) {
        Stop-Model
        Remove-Item $marker -ErrorAction SilentlyContinue
    }

    # 3) unload after idle timeout
    if (Test-Path $marker) {
        $lastUse = (Get-Item $marker).LastWriteTime
        if ((Get-Date) - $lastUse -gt [TimeSpan]::FromSeconds($IdleSeconds)) {
            Stop-Model
            Remove-Item $marker -ErrorAction SilentlyContinue
        }
    }

    Start-Sleep -Seconds $PollSeconds
}
