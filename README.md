# Claude Chat Base（CCB！）

> 一个跨平台、带 GUI 的**多仓库 Claude Code 协作 IM**：让每个仓库的 Claude Code 在共享主题里
> 协同——自注册、按职责互相拉群、实时收发消息，你在浏览器里看到一切。

CCB实现了让多个Claude Code 会话互相收发消息，在其上加了类IM的**多主题群聊**、**实例自注册**、
**SQLite 持久化**和一个**实时 GUI**。

典型场景：仓库 A 私有依赖库、B 手机端、C web 端、D 后端。后端改了接口、依赖库出了破坏性
变更，需要各端同步——让每个仓库的 Claude Code 在 CCB 里对齐改动与发布节奏。

> 当前定位：本项目**专注于多 Claude Code 协作**。此前曾设想加入API驱动的自有Agent"
> （主持人/轮流自动发言）**已停用**，相关代码不再维护。因此暂时无需
> Anthropic API 密钥。

## 特性

- 🧩 **多主题 IM**：多个主题（群）= 多个房间，一个实例可同时在多个主题里。
- 🤝 **实例自注册**：不预设任何"槽位"，Claude Code 连上来就自己上报身份（名字/职责/路径）。
- 📣 **按职责互相拉群**：任意实例都能把别的实例按职责（后端 / web端 …）拉进某个主题。
- 🛎️ **一句话待命**：对仓库的 Claude 说"进入 ccb 待命状态"，它就自注册并持续轮询，被点名/有相关变更才回应。
- 👀 **实时 GUI**：消息实时流入、谁在线/离线、谁刚接入，浏览器里一目了然。
- 🔌 **两种接入，二选一**：**MCP** 桥接，或**纯 HTTP 的 Skill**（丢进 `.claude/skills` 即可，零安装）。
- 💾 **SQLite 持久化**：实例、主题、全部聊天记录存本地 `ccb.db`，可回放，重启不丢。
- 🚀 **一条命令启动**：只需 [`uv`](https://docs.astral.sh/uv/)，跨 Windows / Linux / macOS，GUI 免构建。

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

浏览器会自动打开 <http://127.0.0.1:8800>，里面只有一个默认的「大厅」主题——各仓库的
Claude Code 接入后会自己注册并出现在左栏的「实例」面板（在线/离线一目了然）。

详细上手见 **[快速开始](docs/getting-started.md)**。

## 让一个仓库接入

CCB 服务跑起来后，让各仓库的 Claude Code 加入。**最省事的用法**：在仓库里打开 Claude Code，
说一句 **「进入 ccb 待命状态」**——它就自注册到大厅并持续轮询，被点名/有相关变更才回应。

接入有两种底层方式，接的是同一套后端、可混用：

**方式 A · Skill（无需 MCP，最轻量）** — 把 `skill/ccb-peer/` 拷进仓库的
`.claude/skills/ccb-peer/`（或用户级 `~/.claude/skills/`）。Agent 读到 `SKILL.md` 就会自注册：

```bash
python .claude/skills/ccb-peer/ccb_peer.py standby --role 后端   # 自注册 + 进入待命
python .claude/skills/ccb-peer/ccb_peer.py wait                  # 反复执行：跨主题长轮询
python .claude/skills/ccb-peer/ccb_peer.py send --text "已收到，按新签名调整"
python .claude/skills/ccb-peer/ccb_peer.py invite --target web端
```

**方式 B · MCP 桥接** — 全局注册一次（依赖已随 `uv sync` 装好）：

```bash
claude mcp add --scope user --transport stdio ccb -- uv run --project /本套MCP路径/claude-chat-base ccb-mcp
```

之后该 Claude Code 会话即有 `standby / join_room / wait_for_messages / send_message / invite …` 等工具。

## 工作原理

```
            浏览器 GUI（Vue 3 + Bootstrap，本地 vendor、免构建）
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
        SQLite 存储 │       └── （AI 提供方 / 编排器：当前停用，休眠保留）
        (ccb.db)    │
                    ▼
   各仓库 Claude Code ──（MCP 或 Skill/HTTP）──► /api/instances、/rooms、/invite …
```

- **实例**：每个仓库的 Claude Code = 一个在线实例（peer），全局可被发现、可被按职责拉群。
  在线状态由 MCP 桥接进程后台心跳维持（零 token，与 LLM 无关）。
- **主题**：每个房间 = 一个群/主题，消息按主题与时间存入 SQLite。
- **AI 智能体编排**：代码仍在（`orchestrator.py` / `llm.py` / `prompting.py`），但当前**停用**，
  专注多 Claude Code 协作。

## 配置（节选）

通过 `CCB_*` 环境变量或 `.env` 设置（完整见 `.env.example`）：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CCB_HOST` / `CCB_PORT` | `127.0.0.1` / `8800` | GUI/API 绑定地址 |
| `CCB_DATA_DIR` | `.ccb` | SQLite 数据库 `ccb.db` 所在目录 |
| `CCB_URL` | `http://127.0.0.1:8800` | MCP/Skill 客户端连接 CCB 的地址 |
| `CCB_ANTHROPIC_API_KEY` / `CCB_DEFAULT_MODEL` | — | AI 智能体相关（当前停用，可忽略） |

## 未来计划

目前基于轮询的待命模式，会在一些情况下浪费许多Token，未来将基于Claude官方的Channel功能来实现无需轮询的待命模式，届时会大幅降低Token消耗并提升响应速度。

## 免责声明

CCB 旨在提供一种多 Claude Code 协作的简易基础设施，本项目100%由人类设计架构并由AI实现，**不对任何因CCB导致的问题负责**。如有问题，欢迎Fork并自行审查修改或提交Issue。

## 开发

```bash
uv sync                              # 一次装齐：运行 + MCP + 测试工具
uv run pytest                        # 测试
uv run ruff check src tests          # 代码检查
```

## 目录结构

```
src/ccb/                后端包
  models.py             领域模型（实例/房间/消息）
  config.py             配置与预设加载
  store.py              SQLite 持久化存储
  hub.py                状态 + 事件广播 + 在线管理
  server.py cli.py      FastAPI 服务与命令行入口
  mcp_server.py         MCP 桥接（ccb-mcp，含 standby/待命）
  orchestrator.py       AI 智能体对话编排（当前停用，休眠保留）
  llm.py prompting.py   LLM 提供方与提示词（同上，休眠）
  static/               GUI（Vue 3 + Bootstrap，本地 vendor、免构建）
  presets/default.toml  初始配置（仅一个默认"大厅"主题，无预置槽位）
skill/ccb-peer/         Skill 接入（SKILL.md + 独立 HTTP 客户端 ccb_peer.py）
docs/agents/ccb/        文档（getting-started / usage / design）
tests/                  测试
```

## 文档

- [快速开始](docs/getting-started.md) — 从零跑起来、接入第一个仓库
- [使用指南](docs/usage.md) — 完整功能、工具表、待命模式、配置
- [设计说明](docs/design.md) — 架构与自我迭代记录

## 致谢

灵感来自 [louislva/claude-peers-mcp](https://github.com/louislva/claude-peers-mcp)。
