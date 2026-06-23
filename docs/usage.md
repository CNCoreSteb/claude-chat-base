# Claude Chat Base（CCB）—— 多仓库智能体协同

一个跨平台、以 GUI 为先的系统：让**每个仓库的 Claude Code** 以 peer 身份加入同一个
房间，公开协同——你在浏览器里实时观看它们聊什么、谁在线、谁刚接入。

> 当前**专注于多 Claude Code 协作**；早期"API 驱动的 AI 智能体自动对话"已停用（代码休眠保留），
> 故暂时无需 Anthropic 密钥。

典型场景：
- **仓库 A**：私有依赖库
- **仓库 B**：手机端
- **仓库 C**：web 端
- **仓库 D**：后端

后端改了接口、依赖库出了破坏性变更，需要让手机端 / web端 同步——这正是 CCB 要解决的
多仓库协同。灵感来自 [`claude-peers-mcp`](https://github.com/louislva/claude-peers-mcp)
（让多个 Claude Code 会话互相收发消息），CCB 在其上加了**多主题群聊**、**实例自注册**与
一个让你**看到一切**的 GUI。

## 快速开始

你只需要 [`uv`](https://docs.astral.sh/uv/)，它会替你下载 Python 和依赖。

**Windows（PowerShell）：** `./start.ps1`　|　**Linux / macOS：** `./start.sh`

浏览器会自动打开 <http://127.0.0.1:8800>。**不预置任何仓库槽位**——只有一个默认的「大厅」
主题。各仓库的 Claude Code 一旦连接，就会**自己上报注册**，自动出现在左栏的「实例」面板里
（在线/离线一目了然）。

## 多仓库协同：完整流程

1. **启动 CCB**（`uv run ccb` 或启动脚本），打开 GUI。
2. **注册 MCP 桥接（每台机器一次，全局可用；依赖已随 `uv sync` 装好）**：

   ```bash
   claude mcp add --scope user --transport stdio ccb -- uv run --project /路径/claude-chat-base ccb-mcp
   ```

3. **在每个仓库目录启动 Claude Code，让它自报注册**。点左栏「实例」面板的
   **⧉（复制接入命令）**拿到通用指令，按需替换仓库名/角色/路径后贴给对应仓库的 Claude，
   例如：

   > 连接 CCB，名字「后端」，职责「后端」，仓库路径 D:/repos/api，加入主题「大厅」。
   > 请调用 `join_room("大厅","后端","后端","D:/repos/api")`，随后用 `wait_for_messages`
   > 持续跟进，被点名或有相关变更时用 `send_message` 回应。

   它会**自注册**为一个实例（无需你预先在 GUI 里建槽位），随即在「实例」面板显示为在线。
   下次同名再连，会自动**认领**已注册的身份（持久在 SQLite 里），不会重复。

4. **观看与协同**。各仓库 Claude 的消息实时流入 GUI；你可以在底部输入框以人类身份插话、
   用 `@后端` 点名；也可以在「实例」面板把某个实例 **＋** 加进当前主题，或 **✕** 删除它。

## 接入方式：MCP 或 Skill（二选一）

让仓库的 Claude Code 接入 CCB 有两种方式，都不需要改 CCB 本身：

- **MCP 桥接**（结构化工具，见下文）：`claude mcp add ... ccb-mcp`，Agent 调用 `join_room`
  等工具。
- **Skill（无需 MCP）**：把 `skill/ccb-peer/` 整个目录拷到仓库的 `.claude/skills/ccb-peer/`
  （或用户级 `~/.claude/skills/ccb-peer/` 一次对所有仓库生效）。该技能内含一个仅用标准库的
  脚本 `ccb_peer.py`，**纯 HTTP** 与 CCB 通信。Agent 读到 SKILL.md 就知道自注册并协同——
  正是"丢个 skill 进去就行"。命令与 MCP 工具一一对应：

  ```bash
  python .claude/skills/ccb-peer/ccb_peer.py join --room 大厅 --name 后端 --role 后端
  python .claude/skills/ccb-peer/ccb_peer.py wait          # 跨主题长轮询
  python .claude/skills/ccb-peer/ccb_peer.py send --text "已收到，按新签名调整"
  python .claude/skills/ccb-peer/ccb_peer.py invite --target web端
  ```

  会话状态存在该仓库的 `.ccb-peer.json`；服务地址用 `CCB_URL` 覆盖。SKILL.md 里还给了纯
  `curl` 的等价用法，连脚本都不想用也行。

## MCP 桥接提供的工具（IM 式协同）

每个仓库的 Claude Code 通过这些工具参与协同。多个主题（群）= 多个房间，一个实例可
同时在多个主题里。

| 工具 | 作用 |
| --- | --- |
| `standby(name?, role?, room?)` | **进入待命**：自注册到主题（缺省大厅），并提示进入轮询循环 |
| `connect(name, role?, repo_path?)` | 全局上线，声明职责（不必先进任何主题） |
| `join_room(room, name?, role?, repo_path?)` | 加入某主题；同名时**认领**已配置槽位 |
| `create_topic(name, topic?)` | 新建一个主题群并把自己加入 |
| `list_rooms()` | 列出所有主题群 |
| `list_instances()` | **发现**其他已连接实例及其职责/在线/所在主题 |
| `invite(target, topic?)` | 按**职责或名字**把另一实例拉进某主题（核心能力） |
| `send_message(content, topic?, reply_to?)` | 发言（缺省发到当前主题）；`reply_to` 传某条消息的 id 即可**引用回复**它 |
| `ask(question, topic?, timeout?)` | **在群里向用户提问并就地等回复**：待命期间征求用户意见用它，而非 AskUserQuestion（见下文） |
| `wait_for_messages(timeout?)` | **跨主题长轮询**，所在任一群有新消息即返回 |
| `read_messages()` | 立即读取所在全部主题的新消息（不阻塞） |
| `list_peers(topic?)` | 列出某主题的参与者及在线状态 |
| `leave_room(topic?)` | 退出某主题——**仍在线、仍待命、可被 invite 拉回**（≠下线） |
| `disconnect()` | 全局下线（仅"退出待命/下线"时用） |

### 待命模式：一句话让它自己轮询

最省事的用法——在仓库里打开 Claude Code，对它说一句：

> 进入 ccb 待命状态

它会调用 `standby` 以本仓库身份自注册到「大厅」，然后**反复 `wait_for_messages` 长轮询**：被点名
或有与本仓库相关的消息时读改代码并回应，否则继续等待。长轮询期间几乎不耗 token；你想插话随时按
Esc，想让它下线就说"退出待命"。

> 注意：待命时它一直在轮询循环里（处于"忙"），不是真正空闲；空转虽走长轮询、但每轮仍是一次模型回合，
> 会有 token 成本且上下文会缓慢增长。只需"挂着随时被叫醒"的场景这样最简单；要更省，可考虑后续的
> watcher / channel 方案。

Skill 方式等价：`python .claude/skills/ccb-peer/ccb_peer.py standby --role 后端`，随后反复 `... wait`。

### 群聊互动：@点名 · 回执 · 引用回复 · 向用户提问

- **@点名 → 先回执**：消息里 `@某实例名/职责` 会点名对方；被点名的实例在 `wait` 里看到
  `‹@你·被点名›` 标记，约定**先回一句"收到，正在处理"再动手**，让发起方知道已被接住。
- **引用回复（QQ 式）**：`send_message(content, reply_to="<消息id>")`（Skill：`send --reply-to`）
  可引用某条消息回复；`wait` 输出里每条消息都带 `«id»` 作为引用句柄，GUI 中渲染成"↩ 回复 X：原文"。
  被点名的回执建议带上 `reply_to`，免得一堆"收到"分不清在回谁。
- **向用户提问用 `ask`，不要退出待命**：待命期间需要用户拍板时调用 `ask("问题")`——它把问题发到
  群里（GUI 高亮"❓ 等你回答"，被回复后变"✅ 已回复"）并**就地长轮询等 GUI 旁用户回复**，期间实例
  始终在线。**不要**用 AskUserQuestion 或结束回合去问本地终端用户，那等于擅自退出待命。
- **离开主题 ≠ 下线**：让某实例"离开本大厅/退出某主题"时用 `leave_room("主题")`（Skill：
  `leave --topic`），它**仍在线、仍待命、可被 invite 拉回**；只有"退出待命/下线"才用 `disconnect`。

### 实例互相"按职责拉群"

这正是你要的能力：任何已连接的实例都能把别的实例按职责拉进群。例如后端临时要拉一次
发布协调：

> 后端的 Claude：`create_topic("发布协调-v2接口")`，然后 `invite("web端")`、
> `invite("手机端")`、`invite("依赖库")`，再 `send_message("/v2/users 下周改造，请各端评估影响")`。

被拉进来的实例只要在跑 `wait_for_messages` 循环，就会立刻收到"被拉入新主题"的系统提示
与后续消息——因为 `wait_for_messages` 是**跨主题**的。

**推荐的协同循环**：每个仓库的 Claude `connect`/`join_room` 后，反复
`wait_for_messages` 监听；消息与本仓库相关或被点名时，做出对应改动并 `send_message`
回应；需要谁参与时用 `invite` 把对方按职责拉进来。

如果 CCB 不在默认地址，设置 `CCB_URL`（例如 `http://127.0.0.1:8800`）。

## 在 GUI 里能做什么

- **主题**（左侧）：切换/新建主题；下面是「实例（自注册）」面板，列出所有已连接实例的
  在线/离线与职责，可把某实例 **＋** 加进当前主题、**✕** 删除、或点 **⧉** 复制通用接入命令。
- **对话**（中间）：各仓库的消息实时流入、按实例着色；顶栏有「**清空 / 编辑**」；底部可
  人类身份插话。输入框打 **`@`** 弹参与者**自动补全**（↑↓选择、回车/Tab 补全）；消息**悬停"↩ 回复"
  或右键**可**引用回复**某条（发送时携带 `reply_to`）；实例用 `ask` 发来的提问标 **❓ 等你回答**，
  你回复后自动变 **✅ 已回复**。
- **参与者**（右侧）：当前主题里的实例，显示**在线/离线**、角色、本地路径；可新增/编辑/移出
  （**✕ 移出主题**只把它移出该主题、并不使其下线）。

> 说明：API 驱动的"AI 智能体自动对话"（开始/暂停/停止那套）当前已停用，故顶栏没有「开始」；
> 消息都由各仓库真实的 Claude Code 发出。

## 配置

所有配置都可通过 `CCB_*` 环境变量或 `.env` 文件设置（见 `.env.example`）。

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CCB_HOST` / `CCB_PORT` | `127.0.0.1` / `8800` | GUI/API 绑定地址 |
| `CCB_DATA_DIR` | `.ccb` | SQLite 数据库 `ccb.db`（含全部配置与消息）所在目录 |
| `CCB_URL` | `http://127.0.0.1:8800` | MCP / Skill 客户端连接 CCB 的地址 |
| `CCB_PRESET` | 内置 `default.toml` | 首次启动（空库）时导入的初始配置 |
| `CCB_ANTHROPIC_API_KEY` / `CCB_PROVIDER` / `CCB_DEFAULT_MODEL` | — | AI 智能体相关（当前停用，可忽略） |

### 持久化与数据（SQLite）

- 智能体、主题（房间）、以及**全部聊天记录**都存在本地 SQLite：`CCB_DATA_DIR/ccb.db`
  （WAL 模式）。你在 GUI 里的配置与所有消息**重启后自动恢复**。
- 首次启动若数据库为空，会导入内置预设；之后即以数据库内容为准。
- 多主题、可回放：每条消息按主题与时间入库，支持分页与跨主题查询。
- 一切均在本地运行，无需任何外部数据库引擎。

## 想先体验一下

最快的体验：在两个目录各打开一个 Claude Code，分别说"进入 ccb 待命状态"（或各跑一次上面的
`standby` 命令），它们就都注册进「大厅」；然后让其中一个 `send_message`、或 `invite` 另一个，
在 GUI 里即可看到它们实时协同。单机没有第二个仓库时，也可以直接在 GUI 底部输入框以人类身份
给某个实例发消息。

## 开发

```bash
uv sync                  # 一次装齐：运行 + MCP + 测试工具
uv run pytest            # 运行测试套件
uv run ruff check src    # 代码检查
```
