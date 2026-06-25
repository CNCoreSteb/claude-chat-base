"""SQLite 持久化存储（IM 风格的本地聊天底座）。

智能体、房间、消息都存入一个本地 SQLite 文件（默认 ``<数据目录>/ccb.db``）：

* ``agents`` / ``rooms``：以 JSON 形式存配置（不含运行期字段），同时在内存里保留一份
  镜像以便频繁、快速地访问。
* ``messages``：每条消息一行，支持按房间/时间分页查询与跨房间查询——这是把它做成
  "多主题、可回放"的 IM 所需要的。

并发：本地单用户场景下，用一个开启 WAL 的连接配合一把锁即可既简单又稳健。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from pathlib import Path

from .models import Agent, Message, Room

# 不写入配置（DB）的运行期字段——重启后应回到默认值。
_AGENT_RUNTIME_FIELDS = {"online", "last_seen"}
_ROOM_RUNTIME_FIELDS: set[str] = set()

RECENT_LIMIT = 120  # 快照/最近消息默认返回的条数
HISTORY_LIMIT = 200  # 构造提示词时读取的最近历史条数


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "ccb.db"

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

        # 内存镜像（小、读多写少）：智能体与房间。
        self.agents: dict[str, Agent] = {}
        self.rooms: dict[str, Room] = {}
        self._load_state()

        # 单调、唯一的消息时间戳：从历史最大 ts 起步，写入时保证严格递增（必要时跳到
        # 下一个可表示的浮点）。这样长轮询的 ``ts > since`` 游标永远不会因为两条消息撞上
        # 同一个 time.time()（Windows 时钟分辨率约 1~16ms）而漏发其中之一。
        with self._lock:
            row = self._conn.execute("SELECT MAX(ts) AS m FROM messages").fetchone()
        self._last_ts = float(row["m"]) if row and row["m"] is not None else 0.0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----- schema / 加载 ------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rooms  (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id          TEXT PRIMARY KEY,
                    room_id     TEXT NOT NULL,
                    sender_id   TEXT,
                    sender_name TEXT,
                    role        TEXT,
                    content     TEXT,
                    ts          REAL,
                    color       TEXT,
                    meta        TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_messages_room_ts ON messages(room_id, ts);
                """
            )
            self._conn.commit()

    def _load_state(self) -> None:
        with self._lock:
            for row in self._conn.execute("SELECT data FROM agents ORDER BY rowid"):
                agent = Agent(**json.loads(row["data"]))
                self.agents[agent.id] = agent
            for row in self._conn.execute("SELECT data FROM rooms ORDER BY rowid"):
                room = Room(**json.loads(row["data"]))
                self.rooms[room.id] = room

    # ----- 智能体 -------------------------------------------------------------

    def add_agent(self, agent: Agent) -> Agent:
        return self.upsert_agent(agent)

    def upsert_agent(self, agent: Agent) -> Agent:
        data = {k: v for k, v in agent.model_dump().items() if k not in _AGENT_RUNTIME_FIELDS}
        with self._lock:
            self._conn.execute(
                "INSERT INTO agents(id, data) VALUES(?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (agent.id, json.dumps(data, ensure_ascii=False)),
            )
            self._conn.commit()
        self.agents[agent.id] = agent
        return agent

    def remove_agent(self, agent_id: str) -> None:
        self.agents.pop(agent_id, None)
        with self._lock:
            self._conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            self._conn.commit()
        for room in self.rooms.values():
            if agent_id in room.agent_ids:
                room.agent_ids.remove(agent_id)
                self.upsert_room(room)

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    # ----- 房间 --------------------------------------------------------------

    def add_room(self, room: Room) -> Room:
        return self.upsert_room(room)

    def upsert_room(self, room: Room) -> Room:
        data = {k: v for k, v in room.model_dump().items() if k not in _ROOM_RUNTIME_FIELDS}
        with self._lock:
            self._conn.execute(
                "INSERT INTO rooms(id, data) VALUES(?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (room.id, json.dumps(data, ensure_ascii=False)),
            )
            self._conn.commit()
        self.rooms[room.id] = room
        return room

    def get_room(self, room_id: str) -> Room | None:
        return self.rooms.get(room_id)

    def remove_room(self, room_id: str) -> None:
        """删除一个主题（房间）及其全部消息。"""
        self.rooms.pop(room_id, None)
        with self._lock:
            self._conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
            self._conn.execute("DELETE FROM messages WHERE room_id=?", (room_id,))
            self._conn.commit()

    def rooms_for_agent(self, agent_id: str) -> list[Room]:
        return [r for r in self.rooms.values() if agent_id in r.agent_ids]

    # ----- 消息 ---------------------------------------------------------------

    def add_message(self, message: Message) -> Message:
        with self._lock:
            # 保证 ts 严格单调递增且唯一（见 __init__）：相同 ts 的两条消息会让 since 游标漏发
            # 其中之一；这里把它顶到下一个可表示的浮点。即便系统时钟回拨也仍然递增。
            if message.ts <= self._last_ts:
                message.ts = math.nextafter(self._last_ts, math.inf)
            self._last_ts = message.ts
            self._conn.execute(
                "INSERT INTO messages(id, room_id, sender_id, sender_name, role, content, ts,"
                " color, meta) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    message.id,
                    message.room_id,
                    message.sender_id,
                    message.sender_name,
                    message.role,
                    message.content,
                    message.ts,
                    message.color,
                    json.dumps(message.meta, ensure_ascii=False),
                ),
            )
            self._conn.commit()
        return message

    def get_message(self, message_id: str) -> Message | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
        return _row_to_message(row) if row else None

    def history(self, room_id: str, limit: int = HISTORY_LIMIT) -> list[Message]:
        """按时间正序返回某房间最近 ``limit`` 条消息。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE room_id=? ORDER BY ts DESC, rowid DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    def recent(self, room_id: str, limit: int = RECENT_LIMIT) -> list[Message]:
        return self.history(room_id, limit)

    def messages_since(self, room_id: str, ts: float) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE room_id=? AND ts>? ORDER BY ts, rowid",
                (room_id, ts),
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def messages_for_rooms_since(self, room_ids: list[str], ts: float) -> list[Message]:
        if not room_ids:
            return []
        placeholders = ",".join("?" for _ in room_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM messages WHERE room_id IN ({placeholders}) AND ts>? "
                "ORDER BY ts, rowid",
                (*room_ids, ts),
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def clear_messages(self, room_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE room_id=?", (room_id,))
            self._conn.commit()

    # ----- 只读调试统计（供调试页；都用单连接+锁、只走廉价索引聚合，不扫全表内容）-----------

    @property
    def last_ts(self) -> float:
        return self._last_ts

    def lock_locked(self) -> bool:
        return self._lock.locked()

    def messages_tail(self, limit: int = 50) -> list[Message]:
        """按时间倒序返回最近 limit 条（跨房间），newest first。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def db_stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            rows = self._conn.execute(
                "SELECT room_id, COUNT(*) AS n, MIN(ts) AS mn, MAX(ts) AS mx "
                "FROM messages GROUP BY room_id"
            ).fetchall()
            mx = self._conn.execute("SELECT MAX(ts) AS m FROM messages").fetchone()["m"]
            n_agents = self._conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
            n_rooms = self._conn.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"]
        known = set(self.rooms.keys())
        per_room = [
            {"room_id": r["room_id"], "count": r["n"], "min_ts": r["mn"], "max_ts": r["mx"]}
            for r in rows
        ]
        return {
            "total_messages": total,
            "per_room_counts": per_room,
            "orphan_room_ids": [r["room_id"] for r in rows if r["room_id"] not in known],
            "db_max_ts": mx,
            "last_ts_cursor": self._last_ts,
            "agents_in_db": n_agents, "rooms_in_db": n_rooms,
            "agents_in_memory": len(self.agents), "rooms_in_memory": len(self.rooms),
        }

    def pragma_stats(self) -> dict:
        with self._lock:
            def p(name: str):
                return self._conn.execute(f"PRAGMA {name}").fetchone()[0]
            jm, fk = p("journal_mode"), p("foreign_keys")
            pc, ps, fl = p("page_count"), p("page_size"), p("freelist_count")
        return {"journal_mode": jm, "foreign_keys": fk, "page_count": pc,
                "page_size": ps, "freelist_count": fl, "logical_size_bytes": pc * ps}

    def file_sizes(self) -> dict:
        def sz(path: str) -> int | None:
            try:
                return os.path.getsize(path)
            except OSError:
                return None
        base = str(self.db_path)
        return {"db": sz(base), "wal": sz(base + "-wal"), "shm": sz(base + "-shm")}


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        room_id=row["room_id"],
        sender_id=row["sender_id"],
        sender_name=row["sender_name"],
        role=row["role"],
        content=row["content"],
        ts=row["ts"],
        color=row["color"],
        meta=json.loads(row["meta"] or "{}"),
    )
