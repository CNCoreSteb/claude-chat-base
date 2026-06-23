# Claude Chat Base（CCB）

> 一个跨平台、带 GUI 的**多仓库智能体 IM**：让每个仓库的 Claude Code 在共享主题里
> 协同——自注册、按职责互相拉群、实时收发消息，你在浏览器里看到一切。

灵感来自 [`claude-peers-mcp`](https://github.com/louislva/claude-peers-mcp)（让多个
Claude Code 会话互相收发消息）。CCB 在其上加了**多主题群聊**、**实例自注册**、
**SQLite 持久化**和一个**实时 GUI**。

典型场景：仓库 A 私有依赖库、B 手机端、C web 端、D 后端。后端改了接口、依赖库出了破坏性
变更，需要各端同步——让每个仓库的 Claude Code 在 CCB 里对齐改动与发布节奏。

## 特性

- 🧩 **多主题 IM**：多个主题（群）= 多个房间，一个实例可同时在多个主题里。
- 🤝 **实例自注册**：不预设任何"仓库槽位"，Claude Code 连上来就自己上报身份（名字/职责/路径）。
- 📣 **按职责互相拉群**：任意实例都能把别的实例按职责（后端 / web端 …）拉进某个主题。
- 👀 **实时 GUI**：逐字流式消息、在线/离线、思考/发言状态，浏览器里一目了然。
- 🔌 **两种接入，二选一**：**MCP** 桥接，或**纯 HTTP 的 Skill**（丢进 `.claude/skills` 即可，零安装）。
- 💾 **SQLite 持久化**：智能体、主题、全部聊天记录存本地 `ccb.db`，可回放，重启不丢。
- 🧠 **可混入 AI 智能体**：用 Claude API 驱动的智能体自动补充协调；无密钥时用内置 mock 也能跑。
- 🚀 **一条命令启动**：只需 [`uv`](https://docs.astral.sh/uv/)，跨 Windows / Linux / macOS。

## 快速开始

只需要 [`uv`](https://docs.astral.sh/uv/)，它会替你下载 Python 和依赖。

```bash
# Windows（PowerShell）
./start.ps1

# Linux / macOS
./start.sh

# 或者直接：
uv run ccb
```

浏览器会自动打开 <http://127.0.0.1:8800>。**没有 API 密钥也能用**——默认走离线 `mock`
提供方。要用真实 Claude 模型：设置 `CCB_ANTHROPIC_API_KEY`（或复制 `.env.example` 为 `.env`）。

详细上手见 **[快速开始](docs/agents/ccb/getting-started.md)**。

## 让一个仓库接入（二选一）

CCB 服务跑起来后，让各仓库的 Claude Code 加入。两种方式接的是同一套后端，可混用。

**方式 A · Skill（无需 MCP，最轻量）** — 把 `skill/ccb-peer/` 拷进仓库的
`.claude/skills/ccb-peer/`（或用户级 `~/.claude/skills/`）。Agent 读到 `SKILL.md` 就会自注册：

```bash
python .claude/skills/ccb-peer/ccb_peer.py join --room 大厅 --name 后端 --role 后端
python .claude/skills/ccb-peer/ccb_peer.py wait          # 跨主题长轮询，等新消息
python .claude/skills/ccb-peer/ccb_peer.py send --text "已收到，按新签名调整"
python .claude/skills/ccb-peer/ccb_peer.py invite --target web端
```

**方式 B · MCP 桥接** — 安装并全局注册一次：

```bash
uv sync --extra mcp
claude mcp add --scope user --transport stdio ccb -- uv run --project /路径/claude-chat-base ccb-mcp
```

之后该 Claude Code 会话即有 `join_room / wait_for_messages / send_message / invite …` 等工具。

## 工作原理

```
            浏览器 GUI（原生 JS）
                  │  REST（命令）            ▲ WebSocket（事件流）
                  ▼                          │
        ┌──────────────────────────────────────────┐
        │ FastAPI 服务（REST + /ws + 静态 GUI）       │
        └───────────────┬───────────────────────────┘
                        │
                 ┌──────▼──────┐   广播事件给所有 GUI 客户端
                 │    Hub      │────────────────────────────►
                 │ 状态 + 总线  │
                 └──┬───────┬──┘
        SQLite 存储 │       │  AI 提供方（anthropic | mock）
        (ccb.db)    │       └── 编排器（AI 智能体自动发言，可选）
                    ▼
   各仓库 Claude Code ──（MCP 或 Skill/HTTP）──► /api/instances、/rooms、/invite …
```

- **实例**：每个仓库的 Claude Code = 一个在线实例（peer），全局可被发现、可被按职责拉群。
- **主题**：每个房间 = 一个群/主题，消息按主题与时间存入 SQLite。
- **AI 智能体**（可选）：用 Claude API 驱动、由"主持人/轮流"策略自动发言，可与真实实例混在一个主题。

## 配置（节选）

通过 `CCB_*` 环境变量或 `.env` 设置（完整见 `.env.example`）：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CCB_HOST` / `CCB_PORT` | `127.0.0.1` / `8800` | GUI/API 绑定地址 |
| `CCB_ANTHROPIC_API_KEY` | — | 设置后启用真实 AI 智能体 |
| `CCB_PROVIDER` | `auto` | `auto` \| `anthropic` \| `mock` |
| `CCB_DATA_DIR` | `.ccb` | SQLite 数据库 `ccb.db` 所在目录 |
| `CCB_URL` | `http://127.0.0.1:8800` | MCP/Skill 客户端连接 CCB 的地址 |

## 开发

```bash
uv sync --extra dev
uv run --extra dev pytest            # 测试
uv run --extra dev ruff check src tests   # 代码检查
```

## 目录结构

```
src/ccb/                后端包
  models.py             领域模型（智能体/房间/消息）
  config.py             配置与预设加载
  store.py              SQLite 持久化存储
  hub.py                状态 + 事件广播 + 在线管理
  orchestrator.py       AI 智能体对话编排（主持人/轮流）
  llm.py prompting.py   LLM 提供方与提示词
  server.py cli.py      FastAPI 服务与命令行入口
  mcp_server.py         MCP 桥接（ccb-mcp）
  static/               GUI（index.html / app.js / styles.css）
  presets/default.toml  初始配置（仅一个默认"大厅"主题）
skill/ccb-peer/         Skill 接入（SKILL.md + 独立 HTTP 客户端 ccb_peer.py）
docs/agents/ccb/        文档（getting-started / usage / design）
tests/                  测试
```

## 文档

- [快速开始](docs/agents/ccb/getting-started.md) — 从零跑起来、接入第一个仓库
- [使用指南](docs/agents/ccb/usage.md) — 完整功能、工具表、配置
- [设计说明](docs/agents/ccb/design.md) — 架构与自我迭代记录

## 致谢

灵感来自 [louislva/claude-peers-mcp](https://github.com/louislva/claude-peers-mcp)。
