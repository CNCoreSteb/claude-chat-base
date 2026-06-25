"""运行期配置与预设加载。

配置来自环境变量（前缀 ``CCB_``）以及可选的 ``.env`` 文件。预设——即服务端
全新启动时的初始智能体与房间——从一个 TOML 文件加载，这样用户无需改动代码就能
定制自己的"演员阵容"。
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import Agent, Room

log = logging.getLogger("ccb.config")

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PRESET = PACKAGE_DIR / "presets" / "default.toml"


class Settings(BaseSettings):
    """服务端配置，可通过环境变量（``CCB_*``）或 ``.env`` 覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="CCB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8800
    # 调试页端口（env CCB_DEBUG_PORT）：0/缺省=关闭。设为如 8801 即在该端口（强制 127.0.0.1）
    # 启用一个只读为主的「完整状态追踪」调试页，与主服务共享同一个活的 Hub。无鉴权，故默认关闭。
    debug_port: int = 0

    data_dir: Path = Path(".ccb")
    preset: Path = DEFAULT_PRESET
    open_browser: bool = True

    # 应答编排（answer floor）：避免广播问题被多个 peer 一拥而上重复回答。运行期可在 GUI 切换。
    # scope:        off=关闭 / human=仅人类(GUI 用户)提问触发 / broadcast=所有广播问题触发
    # enforcement:  soft=只记录并告知，agent 自觉让行 / hard=本轮内服务端拒绝非 holder 的回答
    floor_scope: str = "human"
    floor_enforcement: str = "soft"


def load_preset(path: Path) -> tuple[list[Agent], list[Room]]:
    """从 TOML 预设文件加载智能体与房间。

    若文件不存在则返回两个空列表，以保证服务端仍可正常启动。
    """
    if not path.exists():
        return [], []

    # 预设损坏不应让整个服务起不来：解析失败时告警并以空状态启动（与缺失文件一致）。
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning("预设 %s 解析失败：%s —— 跳过预设，以空状态启动。", path, exc)
        return [], []

    agents: list[Agent] = []
    name_to_id: dict[str, str] = {}
    for raw in data.get("agents", []):
        agent = Agent(
            name=raw["name"],
            persona=raw.get("persona", ""),
            role=raw.get("role", ""),
            repo_path=raw.get("repo_path", ""),
            color=raw.get("color", Agent.model_fields["color"].default),
            enabled=raw.get("enabled", True),
        )
        agents.append(agent)
        name_to_id[agent.name] = agent.id

    rooms: list[Room] = []
    for raw in data.get("rooms", []):
        # 预设里房间通过名字引用智能体，便于阅读。
        ids = [name_to_id[n] for n in raw.get("agents", []) if n in name_to_id]
        rooms.append(Room(name=raw["name"], topic=raw.get("topic", ""), agent_ids=ids))

    return agents, rooms
