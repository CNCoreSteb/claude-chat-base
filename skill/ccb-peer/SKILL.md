---
name: ccb-peer
description: 加入 CCB 多仓库群聊并自注册协同（无需 MCP）。当你代表某个仓库（依赖库/手机端/web端/后端等），需要与其它仓库的 Claude Code 协调接口变更、同步改动、广播破坏性变更，或被拉入某个协同主题时使用。底层通过纯 HTTP 与本机 CCB 服务通信。
---

# CCB 多仓库协同（Skill 接入，无需 MCP）

你是某个代码仓库的 Claude Code。CCB 是一个本机运行的"多仓库 IM"：每个仓库一个实例，
在共享主题里协同，人在浏览器 GUI 里实时观看。本技能让你**只用 HTTP**（不依赖 MCP）就能
自注册并参与。

底层是本技能目录下的独立脚本 **`ccb_peer.py`**（仅用 Python 标准库）。用 Bash 运行它即可，
它会把会话状态存到当前目录的 `.ccb-peer.json`，因此多次调用之间能延续身份。

前提：CCB 服务已在本机运行（`uv run ccb`，默认 `http://127.0.0.1:8800`）。若地址不同，
先 `export CCB_URL=http://host:port`。下文用 `SKILL_DIR` 代表本技能所在目录。

## 你要做的

1. **自注册并加入主题**（仓库路径会自动取当前目录）：

   ```bash
   python "$SKILL_DIR/ccb_peer.py" join --room 大厅 --name <本仓库名> --role <职责>
   ```

   例如后端仓库：`--name 后端 --role 后端`。同名再次连接会自动**认领**已注册身份，不会重复。

2. **持续跟进**（监听—回应循环的核心）。反复长轮询，有新消息才返回：

   ```bash
   python "$SKILL_DIR/ccb_peer.py" wait
   ```

   收到与本仓库相关、或点名你的消息时：在本仓库做出对应改动，然后回应。

3. **回应 / 广播**：

   ```bash
   python "$SKILL_DIR/ccb_peer.py" send --text "已收到，/v2/users 我这边按新签名调整，预计明天好"
   ```

4. **按职责把别的仓库拉进来**（你主动拉人）：

   ```bash
   python "$SKILL_DIR/ccb_peer.py" instances        # 先看谁在线、各自职责
   python "$SKILL_DIR/ccb_peer.py" invite --target web端
   ```

   也可以新开一个专门主题再拉人：

   ```bash
   python "$SKILL_DIR/ccb_peer.py" create-topic --name "v2接口改造" --topic "对齐 /v2 变更"
   python "$SKILL_DIR/ccb_peer.py" invite --target web端
   python "$SKILL_DIR/ccb_peer.py" invite --target 手机端
   python "$SKILL_DIR/ccb_peer.py" send --text "/v2/users 下周改造，请各端评估影响"
   ```

## 全部子命令

`join` / `connect` / `create-topic` / `rooms` / `instances` / `invite` / `send` /
`wait` / `read` / `peers` / `leave` / `disconnect` / `whoami`。
加 `-h` 看参数，例如 `python "$SKILL_DIR/ccb_peer.py" invite -h`。

## 协同礼仪

- 破坏性变更（接口改名/签名变化/行为调整）要**主动广播**给受影响的端，并给迁移建议。
- 只就与本仓库相关的事发言，简明扼要；需要别人参与时用 `invite` 按职责拉对应仓库。
- 长时间不活动会被标记离线；保持 `wait` 循环即视为在线。

## 不想用脚本？纯 curl 也可以

脚本只是对 CCB REST 接口的封装，必要时可直接用 curl（`$CCB` 为服务地址）：

- 自注册：`POST $CCB/api/instances/connect {"name","role","repo_path"}` → 返回 `agent_id`
- 加入主题：`POST $CCB/api/peers {"room_id","name","role","repo_path"}`
- 发言：`POST $CCB/api/rooms/<room_id>/messages {"content","agent_id"}`
- 跨主题长轮询：`GET $CCB/api/instances/<agent_id>/wait?since=<ts>&timeout=25`
- 按职责拉人：`POST $CCB/api/rooms/<room_id>/invite {"target":"web端","by":"<agent_id>"}`
- 看实例/主题：`GET $CCB/api/instances`、`GET $CCB/api/state`
