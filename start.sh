#!/usr/bin/env bash
# Claude Chat Base 启动脚本（Linux/macOS）。如未安装 uv 会先自动安装，然后启动 GUI。
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "未找到 uv —— 正在安装（https://docs.astral.sh/uv/）…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 同步依赖（uv 会在需要时自动下载合适的 Python）。
uv sync

echo "正在启动 Claude Chat Base…（Ctrl+C 退出）"
exec uv run ccb "$@"
