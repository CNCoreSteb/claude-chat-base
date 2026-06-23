# Claude Chat Base —— 设计说明

本文记录 CCB 的设计与自我迭代过程，便于审阅各项取舍。

## 目标

构建一个智能体群聊系统，要求：

- **跨平台**（Windows + Linux/macOS）；
- **容易启动**；
- **以 GUI 为先**——你能*实时看见*各仓库的 Claude Code 在协同什么；
- 灵感来自 `claude-peers-mcp`（智能体之间互相对话）。

> 演进说明：本文保留完整迭代记录。项目**当前专注于多 Claude Code 协作**——早期"AI 智能体
> 自动对话"已停用、休眠保留，前端已从手写原生 JS 改为 Vue 3 + Bootstrap（见迭代记录 7–9）。

## 与参考项目的区别

`claude-peers-mcp` 是点对点的：每个 Claude Code 会话运行一个 MCP 服务，由一个 broker
守护进程转发直接消息，没有共享房间，也没有 UI。CCB 保留了其精神（独立智能体互相对话），
但为了*可观察性*重新塑形：

| | claude-peers-mcp | CCB |
| --- | --- | --- |
| 拓扑 | 点对点直接消息 | 共享房间（主题）+ 实例注册表 |
| 参与者 | 真实 Claude Code 会话 | 真实 Claude Code 实例（自注册 peer） |
| 驱动 | 各自由人类提示自己的 Claude | 各仓库 Claude 自注册、待命轮询、按职责互相拉群 |
| 可观察性 | CLI / 各会话内部 | 实时 Web GUI |
| 技术栈 | Bun / TypeScript | Python + uv、FastAPI、Vue 3 + Bootstrap（本地 vendor）的 GUI |

CCB 刻意重新实现了 peer 桥接（`ccb-mcp`），让最初的用法——真实 Claude Code 实例加入
对话——依然可用，而且现在能在 GUI 中看到。

## 架构

```
            浏览器 GUI（Vue 3 + Bootstrap，本地 vendor、免构建）
                  │  REST（命令）            ▲ WebSocket（事件）
                  ▼                          │
        ┌──────────────────────────────────────────┐
        │ FastAPI 服务（server.py）                  │
        │   REST 控制面 + /ws 事件流                  │
        └───────────────┬───────────────────────────┘
                        │
                 ┌──────▼──────┐      把事件广播给所有 WS 客户端
                 │    Hub      │──────────────────────────────────►
                 │ 状态 + 总线  │
                 └──┬───────┬──┘
        SQLite 存储 │       └── （AI 提供方 / 编排器：当前停用，休眠保留）
        (store.py,  │
         ccb.db)    ▼
        外部 Claude Code ──（ccb-mcp / Skill，HTTP）──► /api/peers、/instances、/messages …
```

### 关键取舍

- **一切交给 uv。** `uv run ccb` 会引导 Python + 依赖；无需系统 Python。这是"容易启动"
  最大的杠杆，且在 Windows 与 Linux 上完全一致。
- **免构建 GUI（成熟库）。** 前端用 FastAPI 直接托管、本地 vendor 的 **Vue 3 + Bootstrap 5**
  （不走 CDN、离线可用）——没有 Node 工具链、没有打包器。整个应用一句 `uv run` 即可。
  WebSocket 在连接时下发 `snapshot`，之后只发增量事件；REST 负责状态变更，让 GUI 始终如实
  映射服务端状态。早期用手写的原生 ES 模块，后改用成熟库以免自造轮子。
- **SQLite 持久化。** 实例、主题（房间）与全部聊天记录存于本地 `ccb.db`（WAL 模式），
  支持多主题、分页与跨主题查询——做成可回放的 IM 所需要的底座，且无需任何外部数据库
  引擎。早期用过 JSONL+config.json，随着"多主题 IM + 实例发现"的需求改为 SQLite。
- **在线靠进程心跳，不靠模型轮询。** peer 的"在线"由 MCP 桥接**进程**后台心跳维持（零 token、
  与 LLM 无关），服务端把约 45 秒没收到心跳的 peer 标记为离线——避免逼模型空转烧 token 来"保活"。

以下三条属于已**停用**的 AI 智能体编排（代码休眠保留，日后可恢复）：

- **（停用）mock 提供方。** 没有 API 密钥时也能跑通流式/主持人/状态，便于零配置试用与离线测试。
- **（停用）流式 token 经事件总线传输。** 每轮发言依次发 `message_start` → `message_delta*` →
  `message_end`，带来"观看智能体思考"的鲜活感。
- **（停用）主持人 vs 轮流 + 安全护栏。** 主持人模型挑下一个发言者、轮流兜底；`max_turns`/
  停止/暂停避免自主循环失控。

## 迭代记录

1. **点对点 vs 房间。** 照搬参考项目的"直接消息 broker"难以很好地满足"看到智能体在说
   什么"——没有可供渲染的共享界面。于是选择**共享房间 + 编排器**，让对话只有一条记录
   可供观看。
2. **桌面 GUI vs Web GUI。** Electron/Tauri 带来构建复杂度与逐平台打包问题。**由后端
   托管的浏览器 GUI** 最便携、也最易启动，因此胜出。
3. **框架前端 vs 零构建。** React/Vite 是"最佳实践"，但构建期需要 Node，与"容易启动"
   相冲突。结构良好的原生 ES 模块应用能把项目压缩到一句 `uv run`，同时保持整洁。
4. **致敬参考项目。** 加入 `ccb-mcp` 桥接，让真实 Claude Code peer 仍可加入——把回路接
   回到 `claude-peers`。
5. **转向"多仓库 peer 优先"。** 根据真实用途（仓库 A 依赖库 / B 手机端 / C web端 /
   D 后端 协同），把重心从"AI 人设辩论"转为"每个仓库一个真实 Claude Code peer"。为此：
   - 智能体增加 `role`（仓库角色）/`repo_path`/`online`/`last_seen`，GUI 可配仓库槽位并
     显示在线状态与"复制接入命令"；
   - `join_room` 支持**认领** GUI 里预配的同名槽位；新增 `wait_for_messages` 长轮询，
     形成高效的"监听—回应"协同循环；新增 `leave_room`；
   - GUI 中配置的智能体/房间**持久化**到 `config.json`，重启仍在；
   - 在线状态由 MCP 桥接进程后台心跳维持（零 token，与 LLM 无关），服务端把约 45 秒
     没收到心跳的 peer 标记为离线；
   - 默认预设改为「多仓库协同」房间（依赖库/手机端/web端/后端 四个 peer 槽位）。

   AI 智能体编排（主持人/轮流）依然保留：可在房间里混入 AI 智能体自动补充协调，纯 peer
   房间则不跑自动循环、纯做实时协同空间。
6. **做成多主题 IM + 实例可互相拉群。** 进一步贴合"多仓库 IM"：
   - 存储从 JSONL+config.json 换成 **SQLite**（`ccb.db`），聊天记录与配置统一持久化、
     可回放、可分页与跨主题查询；
   - peer 从"房间内成员"升级为"**全局在线实例**"：`connect` 全局上线、`list_instances`
     发现彼此、`invite` 按**职责(role)**把别的实例拉进任意主题、`create_topic` 开新群；
   - `wait_for_messages` 升级为**跨主题**长轮询，于是"被别人拉入新群"能被即时感知；
   - 快照只下发各主题的近期消息，历史按需查询，避免一次性塞满大量记录。
7. **取消预设槽位，改为自注册。** 不再在预设里预置仓库槽位；各仓库 Claude Code 用 `connect`/
   `join_room`/`standby` 自报身份（同名重连自动认领、持久在 SQLite）。GUI 左栏新增「实例」面板
   显示所有自注册实例与在线状态。
8. **前端改用成熟库（仍免构建）。** 把手写原生 JS 换成本地 vendor 的 **Vue 3 + Bootstrap 5**：
   响应式替代手动重渲染、用现成弹窗/表单/列表组件，仍 `uv run` 一条命令、无需 Node。
9. **停用 AI 自动对话，专注多 Claude Code 协作。** 把 API 驱动的 AI 编排（`orchestrator`/`llm`/
   `prompting`）整体停用、休眠保留；GUI 收起开始/暂停/停止与 AI 入口。在线改由桥接进程心跳维持
   （零 token）；并加"一句话待命"（`standby`）：自注册后持续 `wait_for_messages` 轮询、被点名/
   有相关变更才回应。

## 后续可做

- 房间导出（"把对话存为 Markdown"）。
- 会用工具的智能体（让智能体调用工具，并在 GUI 中显示工具调用）。
- 同时展示多个房间（分屏）。
- 鉴权 + 远程托管方案（当前刻意以 localhost 为先）。
```
