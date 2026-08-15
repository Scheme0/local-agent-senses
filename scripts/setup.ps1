#Requires -Version 5.1
<#
.SYNOPSIS
  local-agent-senses one-click environment setup: checks dependencies, creates
  the config, optionally pulls models, and runs the health check.
.PARAMETER SkipModelPull
  Skip the model download (use when models are already present).
.PARAMETER SkipCheck
  Skip the health check.
.PARAMETER AllowCheckFailure
  Continue when the health check fails instead of exiting nonzero.
.PARAMETER AllAdapters
  Generate adapter files for every known agent tool (default: only detected ones).
#>
[CmdletBinding()]
param(
    [switch]$SkipModelPull,
    [switch]$SkipCheck,
    [switch]$AllowCheckFailure,
    [switch]$AllAdapters
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$config = Join-Path $root "vision-config.json"
$example = Join-Path $root "vision-config.example.json"

Write-Host "== 1/5 Dependency check =="
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "[FAIL] python not found; install Python 3.10+ and add it to PATH"; exit 1 }
Write-Host "[OK] python: $($py.Source)"

$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    $cand = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $cand) { $ollama = Get-Item $cand }
}
if (-not $ollama) {
    Write-Host "[WARN] ollama not found; install it from https://ollama.com/download"
} else {
    Write-Host "[OK] ollama: $($ollama.FullName)"
}

$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ff) {
    Write-Host "[WARN] ffmpeg not found (on PATH). Image analysis works without it; video and audio need it."
} else {
    Write-Host "[OK] ffmpeg: $($ff.Source)"
}

Write-Host "== 2/5 Config =="
if (-not (Test-Path $config)) {
    if (Test-Path $example) {
        Copy-Item $example $config
        Write-Host "[OK] Created vision-config.json; edit the path fields if needed"
    }
} else {
    Write-Host "[OK] vision-config.json already exists"
}

if (-not $SkipModelPull -and $ollama) {
Write-Host "== 3/5 Model download (several GB the first time; use -SkipModelPull to skip) =="
    & $ollama pull haervwe/GLM-4.6V-Flash-9B
    & $ollama pull qwen3.5:4b
}

if ($SkipCheck) {
Write-Host "== 4/5 Health check skipped =="
} elseif ($AllowCheckFailure) {
Write-Host "== 4/5 Health check (AllowCheckFailure; failures continue) =="
    & python (Join-Path $root "vision.py") --check
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARN] health check failed; continuing because -AllowCheckFailure was passed"
    }
} else {
Write-Host "== 4/5 Health check =="
    & python (Join-Path $root "vision.py") --check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "== 5/5 Agent adapters =="
$gen = Join-Path $root "scripts\generate_adapters.py"
if ($AllAdapters) {
    & python $gen --all --out $root
} else {
    & python $gen --out $root
}
Write-Host "Tip: python scripts/generate_adapters.py --all --out <project-dir> generates adapters for every tool"
Write-Host "Done. Example: python vision.py <image> --prompt `"describe the scene`""
