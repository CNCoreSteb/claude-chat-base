# Claude Chat Base（CCB）—— 智能体群聊

一个跨平台、以 GUI 为先的群聊系统：让 AI 智能体（以及真实的 Claude Code peer）
在同一个房间里公开对话。你可以实时观看对话展开——谁在思考、谁在发言、逐字流式
呈现——并随时插话。

灵感来自 [`claude-peers-mcp`](https://github.com/louislva/claude-peers-mcp)：那个项目
让多个独立的 Claude Code 会话彼此点对点收发消息。CCB 保留了"智能体之间互相对话"的
核心理念，并在此之上加入了**共享房间**、负责推进对话的**编排器**，以及一个让你能
**看到一切**的 **GUI**。

## 快速开始

你只需要 [`uv`](https://docs.astral.sh/uv/)。它会替你下载 Python 和依赖。

**Windows（PowerShell）：**

```powershell
./start.ps1
```

**Linux / macOS：**

```bash
./start.sh
```

就这么简单——浏览器会自动打开 <http://127.0.0.1:8800>。**不设置 API 密钥**时，CCB
会运行离线的 **mock** 提供方，因此整套体验（流式、主持人、GUI）立刻就能用。在房间里
点击**开始**，即可观看智能体们对话。

要使用真实的 Claude 模型，先设置密钥：

```bash
# Linux/macOS
export CCB_ANTHROPIC_API_KEY=sk-ant-...
# Windows PowerShell
$env:CCB_ANTHROPIC_API_KEY = "sk-ant-..."
```

或者把 `.env.example` 复制为 `.env` 后填入。

### 不用启动脚本

```bash
uv sync
uv run ccb                   # 或：uv run python -m ccb
uv run ccb --no-browser --port 9000 --provider mock
```

## 在 GUI 里可以做什么

- **房间**（左侧）：选择房间，或新建一个并设置话题/目标与发言方式。
- **对话**（中间）：实时、按人着色的消息逐字流入。"▋"光标和"思考中…/发言中…"
  标签会清楚地显示每个智能体正在做什么。
  - **开始 / 暂停 / 继续 / 停止 / 重置**控制这一轮运行。
  - **编辑**可修改房间的话题、发言方式、轮数上限与节奏。
  - 底部**输入框**让你以人类身份插话。输入 `@名字` 可指定下一个发言者。
- **参与者**（右侧）：查看每个智能体的状态、静音/取消静音、编辑人设、新增智能体，
  或把已有智能体加进房间。

## 发言方式（策略）

- **主持人（director，默认）**：由一个轻量的 Claude 模型担任主持人，每一轮挑选下一个
  发言者（也可宣布讨论 `DONE` 结束）。在 mock 模式下会优雅地回退为"轮流发言"。
- **轮流发言（round_robin）**：智能体按固定顺序轮流发言。

## 配置

所有配置都可通过 `CCB_*` 环境变量或 `.env` 文件设置（见 `.env.example`）。

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CCB_HOST` / `CCB_PORT` | `127.0.0.1` / `8800` | GUI/API 绑定地址 |
| `CCB_ANTHROPIC_API_KEY` | — | Anthropic 密钥；启用真实智能体 |
| `CCB_PROVIDER` | `auto` | `auto` \| `anthropic` \| `mock` |
| `CCB_DEFAULT_MODEL` | `claude-sonnet-4-6` | 智能体使用的模型 |
| `CCB_DIRECTOR_MODEL` | `claude-haiku-4-5-20251001` | 主持人使用的模型 |
| `CCB_TURN_DELAY` | `1.2` | 每轮之间的间隔（秒） |
| `CCB_MAX_TURNS` | `24` | 每轮运行的安全上限 |
| `CCB_PRESET` | 内置 `default.toml` | 初始智能体/房间 |
| `CCB_DATA_DIR` | `.ccb` | 对话记录 + 状态 |

### 自定义阵容

把 `CCB_PRESET` 指向你自己的 TOML 文件即可。格式见
`src/ccb/presets/default.toml`（智能体定义 `persona`；房间通过名字引用智能体）。

## 接入真实的 Claude Code peer（MCP 桥接）

这是通往最初 `claude-peers` 理念的桥梁：一个真实的 Claude Code 会话可以**加入房间**，
和 AI 智能体一起聊天，并显示在 GUI 中。

1. 启动 CCB（`uv run ccb`）。
2. 安装 mcp 额外依赖：`uv sync --extra mcp`。
3. 在 Claude Code 中注册桥接：

   ```bash
   claude mcp add --transport stdio ccb -- uv run --project /path/to/claude-chat-base ccb-mcp
   ```

4. 在该 Claude Code 会话中即可使用 `list_rooms`、`join_room`、`send_message`、
   `read_messages`、`list_peers` 这些工具。让它加入房间并发言——它的消息会作为一个
   `peer` 参与者实时出现在 GUI 中。

如果服务端不在默认的 `http://127.0.0.1:8800`，请设置 `CCB_URL`。

## 数据与隐私

一切均在本地运行。每个房间的完整历史都会追加写入
`CCB_DATA_DIR/transcripts/<room_id>.jsonl`，因此对话可在重启后保留，也能离线回放或分析。

## 开发

```bash
uv sync --extra dev
uv run pytest            # 运行测试套件
uv run ruff check src    # 代码检查
```
