"""命令行入口：``ccb``（以及 ``python -m ccb``）。

用 uvicorn 启动 FastAPI 应用；除非显式禁用，否则在服务端健康后自动用默认浏览器
打开 GUI。
"""

from __future__ import annotations

import argparse
import logging
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from .config import Settings
from .server import create_app


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings()
    # 用 `is not None` 而非真值判断：否则合法的 --port 0（让 OS 选端口）/ --host "" 会被静默丢弃。
    if args.host is not None:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port
    if args.provider:
        settings.provider = args.provider
    if args.model:
        settings.default_model = args.model
    if args.preset:
        settings.preset = Path(args.preset)
    if args.data_dir:
        settings.data_dir = Path(args.data_dir)
    if args.no_browser:
        settings.open_browser = False
    return settings


def _open_browser_when_ready(url: str, health_url: str) -> None:
    def _worker() -> None:
        healthy = False
        for _ in range(100):  # 总共约 10 秒
            try:
                with urllib.request.urlopen(health_url, timeout=1) as resp:
                    if resp.status == 200:
                        healthy = True
                        break
            except (urllib.error.URLError, OSError):
                pass
            threading.Event().wait(0.1)
        if not healthy:
            return  # 服务始终未就绪：不要打开一个打不开的页面
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 无图形界面的环境没有浏览器
            pass

    threading.Thread(target=_worker, daemon=True).start()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ccb", description="Claude Chat Base —— 智能体群聊。")
    parser.add_argument("--host", help="绑定主机（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="绑定端口（默认 8800）")
    parser.add_argument(
        "--provider", choices=["auto", "anthropic", "mock"], help="LLM 提供方"
    )
    parser.add_argument("--model", help="智能体使用的默认模型 id")
    parser.add_argument("--preset", help="智能体/房间预设的 TOML 文件路径")
    parser.add_argument("--data-dir", help="对话记录 / 状态的存放目录")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别")
    args = parser.parse_args(argv)

    # 校验日志级别，避免非法值在 banner/浏览器线程都已启动后才让 uvicorn 崩溃。
    valid_levels = {"critical", "error", "warning", "info", "debug", "trace"}
    if args.log_level.lower() not in valid_levels:
        print(f"  无效的 --log-level「{args.log_level}」，回退到 info。")
        args.log_level = "info"

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = _build_settings(args)
    app = create_app(settings)

    # 0.0.0.0 监听所有网卡；无论如何都让浏览器指向 localhost。
    display_host = "127.0.0.1" if settings.host in ("0.0.0.0", "::", "") else settings.host
    # IPv6 字面量在 URL 里必须用方括号包裹，否则 http://::1:8800 是畸形地址。
    url_host = f"[{display_host}]" if ":" in display_host else display_host
    url = f"http://{url_host}:{settings.port}/"
    health_url = f"http://{url_host}:{settings.port}/api/health"

    banner = (
        "\n"
        "  ┌────────────────────────────────────────────────┐\n"
        "  │  Claude Chat Base —— 智能体群聊                  │\n"
        f"  │  界面：    {url:<37}│\n"
        f"  │  提供方：  {settings.resolved_provider():<37}│\n"
        "  └────────────────────────────────────────────────┘\n"
    )
    print(banner)
    if not settings.anthropic_api_key:
        print("  未检测到 ANTHROPIC API 密钥 —— 正在使用离线的 'mock' 提供方。")
        print("  设置 CCB_ANTHROPIC_API_KEY 即可使用真实的 Claude 模型。\n")

    if settings.open_browser:
        _open_browser_when_ready(url, health_url)

    # 显式构造 Server（而非 uvicorn.run），以便把它存到 app.state：长轮询 / WebSocket
    # 处理器据此感知 should_exit，在 Ctrl+C 时主动退出；timeout_graceful_shutdown 作为兜底，
    # 保证即便有连接未及时收尾，也会在数秒内强制结束（默认 None 会无限等待）。
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()
