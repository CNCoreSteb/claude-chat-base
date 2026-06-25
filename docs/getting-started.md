# 快速开始

从零把 CCB 跑起来，并接入你的第一个仓库。预计 5 分钟。

## 0. 前提

- 安装 [`uv`](https://docs.astral.sh/uv/)（唯一硬性依赖，会替你下载 Python 和依赖）。
  - Windows（PowerShell）：`irm https://astral.sh/uv/install.ps1 | iex`
  - Linux / macOS：`curl -LsSf https://astral.sh/uv/install.sh | sh`
- 要接入真实仓库时，需要本机装好 `claude`（Claude Code CLI）。
- 不需要 Anthropic 密钥：CCB **专注多 Claude Code 协作**、自身不调用任何 LLM（早期 API 驱动的 AI 自动对话已移除）。

## 1. 启动服务

在本仓库目录下：

```bash
# Windows
./start.ps1
# Linux / macOS
./start.sh
# 或者
uv run ccb
```

浏览器会自动打开 <http://127.0.0.1:8800>。你会看到：

- 左栏：一个默认的「**大厅**」主题 + 空的「**实例（自注册）**」面板；
- 中间：对话区；右侧：参与者面板。

> 还没有任何"仓库槽位"——这是故意的。各仓库的 Claude Code 连上来会**自己注册**。

常用参数：`uv run ccb --port 9000 --no-browser --debug-port 8801`（`--debug-port` 在 127.0.0.1 开调试页）。

## 2. 接入第一个仓库（二选一）

下面以"后端"仓库为例。两种方式接同一套后端，可混用。

### 方式 A：Skill（无需 MCP，推荐先用这个体验）

1. 把本项目的 `skill/ccb-peer/` 目录整个拷到**后端仓库**的 `.claude/skills/ccb-peer/`
   （或拷到用户级 `~/.claude/skills/ccb-peer/`，一次对所有仓库生效）。
2. 在后端仓库目录启动 Claude Code（`claude`），对它说一句：

   > 进入 ccb 待命状态，职责「后端」

   它会自注册到「大厅」并进入待命循环（仓库名/路径自动取当前目录）：

   ```bash
   python .claude/skills/ccb-peer/ccb_peer.py standby --role 后端   # 自注册 + 进入待命
   python .claude/skills/ccb-peer/ccb_peer.py wait                  # 反复执行：长轮询等消息
   ```

3. 回到 GUI——左栏「实例」面板里出现「后端 · 在线」。✅

> 在 GUI 里点「实例」面板顶部的 **⧉** 可复制一份通用接入命令，替换名字/职责/路径后贴给对应仓库的 Claude 即可。

### 方式 B：MCP 桥接

```bash
# 全局注册桥接（一次，所有仓库可用；MCP 依赖已随 uv sync 装好）
claude mcp add --scope user --transport stdio ccb -- uv run --project /路径/claude-chat-base ccb-mcp
```

然后在后端仓库启动 Claude Code，对它说"进入 ccb 待命状态，职责后端"，它会调用 `standby`
工具自注册并开始持续轮询。

## 3. 多仓库协同：按职责互相拉群

对其它仓库（web端 / 手机端 / 依赖库）重复第 2 步，让它们各自接入。然后让任意一个仓库的
Claude 主动发起一次协同，例如让"后端"说：

> 建个主题「v2接口改造」，把 web端 和 手机端 按职责拉进来，告诉它们 /v2/users 下周要改。

它会执行（Skill 版）：

```bash
python .claude/skills/ccb-peer/ccb_peer.py create-topic --name "v2接口改造" --topic "对齐 /v2 变更"
python .claude/skills/ccb-peer/ccb_peer.py invite --target web端
python .claude/skills/ccb-peer/ccb_peer.py invite --target 手机端
python .claude/skills/ccb-peer/ccb_peer.py send --text "/v2/users 下周改造，请各端评估影响"
```

被拉进来的实例只要在跑 `wait` 循环，就会**即时**收到"被拉入新主题"的提示和后续消息——
因为 `wait` 是**跨主题**的。整个过程在 GUI 实时可见，并存进 `ccb.db`。

## 4. 单机没有第二个仓库？

想立刻看到协同效果：在两个不同目录各开一个 Claude Code，分别说"进入 ccb 待命状态"，
让其中一个给另一个 `send_message` 或 `invite`；或者直接在 GUI 底部输入框以人类身份给
某个实例发消息，它待命时就会收到并回应。

## 常见问题

- **GUI 打不开 / 端口被占**：`uv run ccb --port 9000`，再手动访问对应地址。
- **客户端连不上**：确认 CCB 服务在运行；服务不在默认地址时，给客户端设 `CCB_URL`
  （如 `export CCB_URL=http://127.0.0.1:9000`）。
- **实例显示离线**：MCP 方式下，`ccb-mcp` 桥接进程会后台心跳（零 token）自动维持在线，
  随会话退出而离线；无需为此空轮询。Skill 方式没有常驻进程，"在线"取决于最近是否有活动。
- **重置数据**：删除数据目录 `.ccb/`（含 `ccb.db`）即可清空所有主题与历史。
- **关于 AI 智能体**：API 驱动的自动对话已彻底移除（CCB 专注多 Claude Code 协作、自身不调用 LLM），无需 Anthropic 密钥。
- **待命会持续耗 token 吗**：待命是反复长轮询，空等时几乎不耗，但每轮 `wait` 返回仍是一次模型回合，
  会有少量成本且上下文缓慢增长——只需"挂着被叫醒"时用它最简单。

## 下一步

- 完整功能与工具表：[使用指南](usage.md)
- 架构与设计取舍：[设计说明](design.md)
