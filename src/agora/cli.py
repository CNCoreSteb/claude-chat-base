"""Command-line entry point: ``agora`` (and ``python -m agora``).

Boots the FastAPI app with uvicorn and, unless disabled, opens the GUI in the
default browser once the server is healthy.
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
    if args.host:
        settings.host = args.host
    if args.port:
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
        for _ in range(100):  # ~10s total
            try:
                with urllib.request.urlopen(health_url, timeout=1) as resp:
                    if resp.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass
            threading.Event().wait(0.1)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless environments have no browser
            pass

    threading.Thread(target=_worker, daemon=True).start()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agora", description="Agora — agent group chat.")
    parser.add_argument("--host", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Bind port (default 8800)")
    parser.add_argument(
        "--provider", choices=["auto", "anthropic", "mock"], help="LLM provider"
    )
    parser.add_argument("--model", help="Default model id for agents")
    parser.add_argument("--preset", help="Path to a TOML preset of agents/rooms")
    parser.add_argument("--data-dir", help="Directory for transcripts/state")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser")
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = _build_settings(args)
    app = create_app(settings)

    # 0.0.0.0 binds all interfaces; point the browser at localhost regardless.
    display_host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    url = f"http://{display_host}:{settings.port}/"
    health_url = f"http://{display_host}:{settings.port}/api/health"

    banner = (
        "\n"
        "  ┌──────────────────────────────────────────────┐\n"
        "  │  Agora — agent group chat                      │\n"
        f"  │  GUI:      {url:<36}│\n"
        f"  │  Provider: {settings.resolved_provider():<36}│\n"
        "  └──────────────────────────────────────────────┘\n"
    )
    print(banner)
    if not settings.anthropic_api_key:
        print("  No ANTHROPIC_API_KEY found — running the offline 'mock' provider.")
        print("  Set AGORA_ANTHROPIC_API_KEY to use real Claude models.\n")

    if settings.open_browser:
        _open_browser_when_ready(url, health_url)

    uvicorn.run(app, host=settings.host, port=settings.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
