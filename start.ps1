# Claude Chat Base 启动脚本（Windows / PowerShell）。如未安装 uv 会先自动安装，然后启动 GUI。
# 注意：本文件以 UTF-8 + BOM 保存，否则 Windows PowerShell 5.1 会按本地代码页解析其中的中文而出现乱码。
$ErrorActionPreference = "Stop"
# 让中文在控制台正确显示（兼容旧代码页）。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Set-Location -Path $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "未找到 uv —— 正在安装（https://docs.astral.sh/uv/）..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# 同步依赖（uv 会在需要时自动下载合适的 Python）。
uv sync

Write-Host "正在启动 Claude Chat Base...（Ctrl+C 退出）"
uv run ccb @args
