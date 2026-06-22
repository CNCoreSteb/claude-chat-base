# Agora launcher for Windows (PowerShell). Bootstraps uv if missing, then starts the GUI.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found — installing it (https://docs.astral.sh/uv/)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# Sync dependencies (uv auto-downloads a suitable Python if needed).
uv sync

Write-Host "Starting Agora...  (Ctrl+C to stop)"
uv run agora @args
