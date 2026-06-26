// Claude Chat Base 界面 —— Vue 3（本地 vendor，免构建）+ Bootstrap 5 组件。
// 通过 WebSocket 接收事件、驱动响应式状态；通过 REST 接口执行操作。
const { createApp } = Vue;

const PALETTE = [
  "#6366f1", "#ec4899", "#10b981", "#f59e0b", "#06b6d4",
  "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6",
];

createApp({
  data() {
    return {
      agents: {},        // id -> 智能体
      rooms: {},         // id -> 主题
      messages: {},      // room_id -> [消息]
      floors: {},        // room_id -> 应答位状态 { holder, holder_name, queue, queue_names, active, ... }
      server: {},
      currentRoomId: null,
      draft: "",
      stick: true,       // 是否贴着底部（决定流式时是否自动滚动）
      unread: 0,         // 滚上去看历史时，期间到达的新消息条数（悬浮"回到最新"箭头上显示）
      toastMsg: "",
      palette: PALETTE,
      // 主题：用预绘制脚本已写入的 data-bs-theme 作为初值，保证切换按钮图标与实际主题一致。
      theme: document.documentElement.getAttribute("data-bs-theme") || "dark",
      // WebSocket 重连：指数退避 + 去重，避免断网时每 1.2s 刷屏并堆叠定时器。
      reconnectDelay: 1200,
      reconnectTimer: null,
      wasConnected: false,
      modal: { title: "", fields: [], values: {}, onSave: null },
      // @提及自动补全：open 是否显示、items 候选、index 高亮项、start 输入框里 @ 的下标。
      mention: { open: false, items: [], index: 0, start: -1 },
      // 正在回复的目标消息（QQ 式引用）：{ id, sender_name, preview }，null 表示不引用。
      replyTo: null,
      // 经 @ 自动补全**明确选中**的参与者：[{ id, name }]。发送时按 agentid 显式带给服务端，
      // 让"这条主要发给谁"不再只靠正文文本解析（重名/措辞都不怕）。
      pickedMentions: [],
      // 三级 TODO（全局/主题/各 agent）、受控请求队列、全局 todo 被授权的 agentid。
      todos: [],
      requests: [],
      globalEditors: [],
      newTodo: { global: "", room: "" },   // 两个输入框的草稿
    };
  },

  computed: {
    roomList() { return Object.values(this.rooms); },
    currentRoom() { return this.rooms[this.currentRoomId] || null; },
    currentMessages() { return this.messages[this.currentRoomId] || []; },
    // 是否显示"回到最新"悬浮箭头：当前主题有消息、且用户已滚上去（未贴底）时显示。
    showJumpLatest() { return !this.stick && this.currentMessages.length > 0; },
    // 当前主题的应答位状态（应答编排）；active 时才在头部显示"谁正在回答/排队"。
    currentFloor() { return this.floors[this.currentRoomId] || null; },
    // 三级 TODO 视图。
    globalTodos() { return this.todos.filter((t) => t.scope === "global"); },
    currentRoomTodos() {
      return this.todos.filter((t) => t.scope === "room" && t.scope_id === this.currentRoomId);
    },
    // 当前主题待审批的受控请求（主持人/你可批/拒）。
    currentRoomRequests() {
      return this.requests.filter((r) => r.room_id === this.currentRoomId && r.status === "pending");
    },
    currentHost() {
      const r = this.currentRoom;
      return r && r.host_id ? (this.agents[r.host_id] || null) : null;
    },
    floorScopeLabel() {
      return ({ off: "关闭", human: "仅我的提问", broadcast: "所有广播问题" })[this.server.floor_scope]
        || this.server.floor_scope;
    },
    directedLabel() {
      return ({ all: "全部可见", recipient: "仅接收者可见", until_reply: "回复后解禁" })[
        this.server.directed_visibility] || this.server.directed_visibility;
    },
    // 被视为"已回复"的提问 id 集合：仅当**用户（human）引用回复了这条提问本身**才算。
    // 不再用"提问之后出现过任何人类发言"来判断——否则用户引用回复其它消息、或发别的与
    // 该提问无关的消息时，会把尚未回答的提问误标为"已回复"。要标记某条提问为已回复，
    // 在 GUI 里对它点"↩ 回复"作答即可。
    answeredQuestionIds() {
      const s = new Set();
      for (const m of this.currentMessages) {
        if (m.role === "human" && m.meta && m.meta.reply_to) s.add(m.meta.reply_to);
      }
      return s;
    },
    roomAgents() {
      const r = this.currentRoom;
      return r ? r.agent_ids.map((id) => this.agents[id]).filter(Boolean) : [];
    },
    peerInstances() {
      return Object.values(this.agents)
        .filter((a) => a.kind === "peer")
        .sort((a, b) => (b.online ? 1 : 0) - (a.online ? 1 : 0));
    },
    availableAgents() {
      const r = this.currentRoom;
      return r ? Object.values(this.agents).filter((a) => !r.agent_ids.includes(a.id)) : [];
    },
  },

  methods: {
    // ----- 展示辅助 -----
    initials(name) {
      const s = (name || "?").trim();
      if (!s) return "?";
      return s.codePointAt(0) > 0x2e7f ? Array.from(s)[0] : s.slice(0, 2).toUpperCase();
    },
    fmtTime(ts) {
      return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    },
    escapeHtml(s) {
      const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
      return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => map[c]);
    },
    // 先转义再高亮 @点名。用 Unicode 属性（\p{L}\p{N}）对齐服务端 Python 的 `@([\w-]+)`
    // （Python \w 是 Unicode 感知的），从而日/韩/带重音等非 CJK 名字也能正确高亮，不再与服务端解析口径不一致。
    renderContent(text) {
      return this.escapeHtml(text).replace(
        /@([\p{L}\p{N}_-]+)/gu, '<span class="mention">@$1</span>',
      );
    },
    statusLabel(a) {
      return (a.role ? a.role + " · " : "")
        + (a.online ? "在线" : "离线（等待 Claude Code 接入）");
    },
    statusClass(a) {
      return a.online ? "text-success" : "text-secondary";
    },

    // ----- 网络 -----
    async api(method, path, body) {
      const res = await fetch(path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        this.toast(`错误：${text}`);
        throw new Error(text);
      }
      return res.status === 204 ? null : res.json();
    },
    connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onmessage = (e) => {
        // 守卫畸形帧：解析失败时忽略这一帧，别让异常打断 onmessage、丢掉后续事件。
        let ev;
        try { ev = JSON.parse(e.data); } catch { return; }
        this.handleEvent(ev);
      };
      ws.onopen = () => {
        this.reconnectDelay = 1200;
        if (this.wasConnected) this.toast("已重连");
        this.wasConnected = true;
      };
      ws.onclose = () => {
        if (this.reconnectTimer) return;            // 已有挂起的重连，避免叠加
        if (this.wasConnected) this.toast("连接断开 —— 正在重连…");  // 仅在「已连接→断开」时提示一次
        this.wasConnected = false;
        this.reconnectTimer = setTimeout(() => {
          this.reconnectTimer = null;               // 必须先清空再重连，否则会永久卡住
          this.connect();
        }, this.reconnectDelay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 2, 15000);  // 指数退避，封顶 15s
      };
      ws.onerror = () => ws.close();
    },

    // ----- 事件 -----
    handleEvent(ev) {
      switch (ev.type) {
        case "snapshot": return this.applySnapshot(ev);
        case "agent_added":
        case "agent_updated": this.agents[ev.agent.id] = ev.agent; break;
        case "agent_removed": delete this.agents[ev.agent_id]; break;
        case "room_added":
        case "room_updated":
          this.rooms[ev.room.id] = ev.room;
          if (!this.currentRoomId) this.selectRoom(ev.room.id);
          break;
        case "room_reset": this.messages[ev.room_id] = []; break;
        case "answer_floor": this.floors[ev.room_id] = ev.floor; break;
        case "floor_config":
          this.server = { ...this.server, floor_scope: ev.scope, floor_enforcement: ev.enforcement,
            directed_visibility: ev.directed_visibility };
          break;
        case "room_removed": {
          const wasActive = this.currentRoomId === ev.room_id;
          delete this.rooms[ev.room_id];
          delete this.messages[ev.room_id];
          delete this.floors[ev.room_id];
          if (wasActive) {
            const next = Object.keys(this.rooms)[0] || null;
            // 复用 selectRoom 做完整重置（replyTo/pickedMentions/stick/unread/滚动）；无主题时手动清空。
            if (next) this.selectRoom(next);
            else { this.currentRoomId = null; this.replyTo = null;
              this.pickedMentions = []; this.closeMention(); }
          }
          break;
        }
        case "message": this.addMessage(ev.message); break;
        case "todo_added": this.todos.push(ev.todo); break;
        case "todo_updated": {
          const i = this.todos.findIndex((t) => t.id === ev.todo.id);
          if (i >= 0) this.todos.splice(i, 1, ev.todo); else this.todos.push(ev.todo);
          break;
        }
        case "todo_removed":
          this.todos = this.todos.filter((t) => t.id !== ev.todo_id); break;
        case "request_added": this.requests.push(ev.request); break;
        case "request_updated": {
          const i = this.requests.findIndex((r) => r.id === ev.request.id);
          if (i >= 0) this.requests.splice(i, 1, ev.request); else this.requests.push(ev.request);
          break;
        }
        case "global_todo_editors": this.globalEditors = ev.editors || []; break;
      }
    },
    applySnapshot(snap) {
      this.agents = Object.fromEntries(snap.agents.map((a) => [a.id, a]));
      this.rooms = Object.fromEntries(snap.rooms.map((r) => [r.id, r]));
      this.messages = snap.messages || {};
      this.floors = snap.floors || {};
      this.server = snap.server || {};
      this.todos = snap.todos || [];
      this.requests = snap.requests || [];
      this.globalEditors = snap.global_todo_editors || [];
      if (!this.currentRoomId || !this.rooms[this.currentRoomId]) {
        this.currentRoomId = snap.rooms[0]?.id || null;
      }
      this.stick = true;
      this.unread = 0;
      // 加载后直接停在最新一条。
      this.scrollToLatestSoon();
    },

    // ----- 消息 -----
    addMessage(msg) {
      const m = { ...msg };
      if (!this.messages[m.room_id]) this.messages[m.room_id] = [];
      const list = this.messages[m.room_id];
      list.push(m);
      // 限制单主题在内存里保留的消息数，避免长会话无界增长（历史仍在服务端，刷新即重新快照）。
      if (list.length > 2000) list.splice(0, list.length - 2000);
      if (m.room_id === this.currentRoomId) {
        if (this.stick) this.$nextTick(() => this.scrollToBottom());
        else this.unread++;   // 用户正在上面看历史：累计未读，悬浮箭头上提示
      }
    },
    onScroll() {
      const t = this.$refs.transcript;
      if (!t) return;
      this.stick = t.scrollHeight - t.scrollTop - t.clientHeight < 120;
      if (this.stick) this.unread = 0;   // 已贴底：清掉"期间新消息"计数
    },
    scrollToBottom() {
      const t = this.$refs.transcript;
      // 用 behavior:auto 瞬时贴底——流式高频更新时若用 smooth 会持续追不上底部而抖动。
      if (t) t.scrollTo({ top: t.scrollHeight, behavior: "auto" });
      this.stick = true;
      this.unread = 0;
    },
    // 加载/切换主题后稳妥地停在最新一条：DOM 更新后滚一次，再在下一帧补一次——防止字体/
    // 布局尚未稳定导致首次 scrollHeight 偏小而没真正贴到底。
    scrollToLatestSoon() {
      this.$nextTick(() => {
        this.scrollToBottom();
        requestAnimationFrame(() => this.scrollToBottom());
      });
    },
    // 点悬浮箭头：平滑回到最新消息。
    jumpToLatest() {
      this.stick = true;
      this.unread = 0;
      const t = this.$refs.transcript;
      if (t) t.scrollTo({ top: t.scrollHeight, behavior: "smooth" });
    },

    // ----- 主题 -----
    selectRoom(id) {
      this.currentRoomId = id;
      this.replyTo = null;            // 切主题：清掉上个主题里选中的回复目标
      this.pickedMentions = [];       // 以及上个主题里选中的 @ 接收者
      this.closeMention();
      this.stick = true;
      this.unread = 0;
      // 切主题直接停在最新一条（稳妥贴底）。
      this.scrollToLatestSoon();
    },
    async roomAction(action) {
      if (this.currentRoom) await this.api("POST", `/api/rooms/${this.currentRoom.id}/${action}`);
    },
    // 应答编排配置（scope: off/human/broadcast，enforcement: soft/hard）。
    setFloorConfig(patch) { this.api("PATCH", "/api/answer-floor", patch).catch(() => {}); },

    // ----- TODO / 主持人 / 受控请求（GUI 即人工管理员，actor=human）-----
    async addTodo(scope, scopeId) {
      const text = (this.newTodo[scope] || "").trim();
      if (!text) return;
      await this.api("POST", "/api/todos",
        { scope, scope_id: scopeId || "", text, actor: "human" }).catch(() => {});
      this.newTodo[scope] = "";
    },
    toggleTodo(t) {
      this.api("PATCH", `/api/todos/${t.id}`, { done: !t.done, actor: "human" }).catch(() => {});
    },
    removeTodo(id) { this.api("DELETE", `/api/todos/${id}?actor=human`).catch(() => {}); },
    agentName(id) { return this.agents[id]?.name || (id === "human" ? "你" : id); },
    setHost(hostId) {
      if (!this.currentRoom) return;
      this.api("PATCH", `/api/rooms/${this.currentRoomId}/host`, { host_id: hostId }).catch(() => {});
    },
    resolveRequest(id, approve) {
      this.api("POST", `/api/requests/${id}/resolve`, { approver: "human", approve }).catch(() => {});
    },
    requestSummary(r) {
      const p = r.payload || {};
      const tgt = p.target_name || p.text || p.todo_id || "";
      return `${r.requested_by_name || r.requested_by} 请求 ${r.action} ${tgt}`;
    },
    grantGlobalEditor(id) {
      if (!id || this.globalEditors.includes(id)) return;
      this.api("PATCH", "/api/global-todo-editors",
        { editors: [...this.globalEditors, id] }).catch(() => {});
    },
    revokeGlobalEditor(id) {
      this.api("PATCH", "/api/global-todo-editors",
        { editors: this.globalEditors.filter((e) => e !== id) }).catch(() => {});
    },
    openSettings() {
      this._settingsOpener = document.activeElement;   // 关闭后把焦点还回触发按钮
      this.bsSettings.show();
    },
    deleteRoom(room) {
      if (!room) return;
      if (confirm(`删除主题「${room.name}」？该主题的全部消息也会一并删除，且不可恢复。`)) {
        this.api("DELETE", `/api/rooms/${room.id}`).catch(() => {});
      }
    },

    // ----- 输入框 -----
    sendMessage() {
      const text = this.draft.trim();
      if (!text || !this.currentRoomId) return;
      const body = { content: text };
      if (this.replyTo) body.reply_to = this.replyTo.id;
      // 仅保留 @名字仍在正文里的选中项，按 agentid 显式带给服务端；首个作为"主要接收者"(to)。
      const picked = this.pickedMentions.filter((p) => text.includes("@" + p.name));
      if (picked.length) {
        body.mentions = picked.map((p) => p.id);
        body.to = picked[0].id;
      }
      this.api("POST", `/api/rooms/${this.currentRoomId}/messages`, body).catch(() => {});
      this.draft = "";
      this.replyTo = null;
      this.pickedMentions = [];
      const el = this.$refs.composer;
      if (el) el.style.height = "auto";
    },
    // ----- 回复指定消息（QQ 式引用）-----
    startReply(m) {
      if (!m || m.role === "system") return;   // 系统提示不可回复
      this.replyTo = {
        id: m.id,
        sender_name: m.sender_name,
        preview: (m.content || "").replace(/\s+/g, " ").slice(0, 80),
      };
      this.$nextTick(() => this.$refs.composer?.focus());
    },
    cancelReply() { this.replyTo = null; },
    autoGrow(e) {
      const el = e.target;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 140) + "px";
    },

    // ----- @提及自动补全（模仿 IM）-----
    onComposerInput(e) {
      this.autoGrow(e);
      // 中文输入法拼音组字途中先不弹菜单，避免回车确认候选时误触发选择。
      if (e.isComposing) return;
      this.updateMention(e.target);
    },
    onComposerKeydown(e) {
      if (e.isComposing) return;                    // 组字中的回车交给输入法
      if (this.mention.open) {
        if (e.key === "ArrowDown") { e.preventDefault(); return this.moveMention(1); }
        if (e.key === "ArrowUp") { e.preventDefault(); return this.moveMention(-1); }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          return this.applyMention(this.mention.items[this.mention.index]);
        }
        if (e.key === "Escape") { e.preventDefault(); return this.closeMention(); }
      }
      // 回车发送，Shift/Ctrl/Alt/Meta + 回车则换行（等价于原 .enter.exact）。
      if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
        e.preventDefault();
        this.sendMessage();
      }
    },
    onComposerBlur() { this.closeMention(); },
    updateMention(el) {
      const pos = el.selectionStart;
      // 直接读 el.value（v-model 更新 draft 可能慢一拍）。光标前文本里，最近一个
      // 「行首或空白后的 @」到光标之间若无空白，即正在输入提及。
      const m = /(?:^|\s)@([^\s@]*)$/.exec(el.value.slice(0, pos));
      if (!m) return this.closeMention();
      const query = m[1].toLowerCase();
      const items = this.roomAgents
        .filter((a) => a.name.toLowerCase().includes(query) || (a.role || "").toLowerCase().includes(query))
        .slice(0, 8);
      if (!items.length) return this.closeMention();
      this.mention = { open: true, items, index: 0, start: pos - m[1].length - 1 };
    },
    moveMention(d) {
      const n = this.mention.items.length;
      if (n) this.mention.index = (this.mention.index + d + n) % n;
    },
    applyMention(agent) {
      if (!this.mention.open || !agent) return;
      const el = this.$refs.composer;
      const pos = el ? el.selectionStart : this.draft.length;
      const before = this.draft.slice(0, this.mention.start);
      const insert = `@${agent.name} `;
      this.draft = before + insert + this.draft.slice(pos);
      // 记下这次明确选中的 agentid，发送时作为显式接收者约束（首个即"主要发给谁"）。
      if (!this.pickedMentions.some((p) => p.id === agent.id)) {
        this.pickedMentions.push({ id: agent.id, name: agent.name });
      }
      this.closeMention();
      this.$nextTick(() => {
        if (!el) return;
        const caret = (before + insert).length;
        el.focus();
        el.setSelectionRange(caret, caret);
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 140) + "px";
      });
    },
    closeMention() {
      this.mention = { open: false, items: [], index: 0, start: -1 };
    },

    // ----- 参与者 -----
    addToRoom(agentId) { this.api("POST", `/api/rooms/${this.currentRoomId}/agents/${agentId}`).catch(() => {}); },
    removeFromRoom(agentId) { this.api("DELETE", `/api/rooms/${this.currentRoomId}/agents/${agentId}`).catch(() => {}); },
    onAddExisting(e) { const id = e.target.value; if (id) this.addToRoom(id); e.target.value = ""; },
    patchAgent(id, patch) { this.api("PATCH", `/api/agents/${id}`, patch).catch(() => {}); },
    deleteInstance(a) {
      if (confirm(`删除实例「${a.name}」？其历史消息会保留。`)) {
        this.api("DELETE", `/api/agents/${a.id}`).catch(() => {});
      }
    },
    kickInstance(a) {
      if (confirm(`踢掉实例「${a.name}」？将强制其下线，并通知其桥接停止心跳；它重新 standby 即可归队。`)) {
        this.api("POST", `/api/peers/${a.id}/kick`).catch(() => {});
      }
    },

    // ----- 接入命令 -----
    async copyText(text, ok) {
      try { await navigator.clipboard.writeText(text); this.toast(ok); }
      catch { this.toast("复制失败，请手动复制"); console.log(text); }
    },
    copyJoin(a) {
      const topic = this.currentRoom ? this.currentRoom.name : "大厅";
      const text =
        `# 在「${a.role || a.name}」仓库目录启动 Claude Code，并注册一次 MCP（如未注册过）：\n` +
        `claude mcp add --scope user --transport stdio ccb -- uv run --project <claude-chat-base 路径> ccb-mcp\n\n` +
        `# 然后对该会话说（或让它执行）：\n` +
        `连接 CCB，名字「${a.name}」，职责「${a.role || ""}」${a.repo_path ? `，仓库路径 ${a.repo_path}` : ""}，加入主题「${topic}」。\n` +
        `请调用 join_room("${topic}", "${a.name}", "${a.role || ""}", "${a.repo_path || ""}")，` +
        `随后用 wait_for_messages 持续跟进，被点名或有相关变更时用 send_message 回应。`;
      this.copyText(text, `已复制「${a.name}」的接入命令`);
    },
    copyGenericJoin() {
      const topic = this.currentRoom ? this.currentRoom.name : "大厅";
      const text =
        `# 1) 注册 MCP（每台机器一次）：\n` +
        `claude mcp add --scope user --transport stdio ccb -- uv run --project <claude-chat-base 路径> ccb-mcp\n\n` +
        `# 2) 在某个仓库目录启动 Claude Code，对它说：\n` +
        `连接 CCB，名字"<仓库名>"，职责"<角色，如 后端/web端>"，仓库路径"<本仓库路径>"，加入主题「${topic}」。\n` +
        `请调用 join_room("${topic}", "<仓库名>", "<角色>", "<仓库路径>")，随后反复 wait_for_messages 跟进；\n` +
        `被点名或有相关变更时用 send_message 回应；需要谁参与时用 invite 按职责把对方拉进来。`;
      this.copyText(text, "已复制通用接入命令");
    },

    // ----- 弹窗 -----
    openModal(title, fields, onSave) {
      const values = {};
      for (const f of fields) values[f.key] = f.value ?? "";
      this.modal = { title, fields, values, onSave };
      this._modalOpener = document.activeElement;   // 记下触发按钮，关闭后把焦点还回去
      this.bsModal.show();
    },
    hideModal() { this.bsModal.hide(); },
    async saveModal() {
      if (!this.modal.onSave) return this.hideModal();
      try { await this.modal.onSave(this.modal.values); this.bsModal.hide(); }
      catch { /* api() 已弹出错误提示，弹窗保持打开 */ }
    },
    newRoom() {
      // AI 自动对话停用：不再设置 发言方式/最大轮数（仅 AI 智能体相关）。
      this.openModal("新建主题", [
        { key: "name", label: "主题名称", value: "" },
        { key: "topic", label: "话题 / 目标", type: "textarea", value: "" },
      ], async (v) => {
        // 新主题默认不预先拉入任何成员，由你按需用「实例」面板或「添加已有」加入。
        const room = await this.api("POST", "/api/rooms", {
          name: v.name || "新主题", topic: v.topic, agent_ids: [],
        });
        this.selectRoom(room.id);
      });
    },
    editRoom(room) {
      this.openModal("编辑主题", [
        { key: "name", label: "主题名称", value: room.name },
        { key: "topic", label: "话题 / 目标", type: "textarea", value: room.topic },
      ], (v) => this.api("PATCH", `/api/rooms/${room.id}`, { name: v.name, topic: v.topic }));
    },
    newAgent() {
      // 每个参与者都是一个外部 Claude Code 实例（仓库 peer 槽位）。
      this.openModal("新增参与者", [
        { key: "name", label: "名称", value: "" },
        { key: "role", label: "仓库角色（如 后端 / web端，可选）", value: "" },
        { key: "repo_path", label: "仓库本地路径（可选）", value: "" },
        { key: "persona", label: "仓库上下文 / 说明（可选）", type: "textarea", value: "" },
        { key: "color", label: "颜色", type: "color", value: PALETTE[Object.keys(this.agents).length % PALETTE.length] },
      ], async (v) => {
        const agent = await this.api("POST", "/api/agents", {
          name: v.name || "仓库", kind: "peer",
          role: v.role, repo_path: v.repo_path, persona: v.persona, color: v.color,
        });
        if (this.currentRoomId) await this.api("POST", `/api/rooms/${this.currentRoomId}/agents/${agent.id}`);
      });
    },
    editAgent(a) {
      this.openModal("编辑参与者", [
        { key: "name", label: "名称", value: a.name },
        { key: "role", label: "仓库角色（如 后端 / web端，可选）", value: a.role || "" },
        { key: "repo_path", label: "仓库本地路径（可选）", value: a.repo_path || "" },
        { key: "persona", label: "仓库上下文 / 说明（可选）", type: "textarea", value: a.persona },
        { key: "color", label: "颜色", type: "color", value: a.color },
      ], (v) => this.api("PATCH", `/api/agents/${a.id}`, {
        name: v.name, role: v.role, repo_path: v.repo_path, persona: v.persona, color: v.color,
      }));
    },

    // ----- 主题（深 / 浅色） -----
    applyTheme(theme, persist) {
      this.theme = theme;
      document.documentElement.setAttribute("data-bs-theme", theme);
      if (persist) {
        try { localStorage.setItem("ccb-theme", theme); } catch (e) { /* 隐私模式：退化为内存态 */ }
      }
    },
    toggleTheme() {
      this.applyTheme(this.theme === "dark" ? "light" : "dark", true);
    },

    toast(msg) { this.toastMsg = msg; this.bsToast.show(); },
  },

  mounted() {
    this.bsModal = new bootstrap.Modal(this.$refs.modal);
    this.bsSettings = new bootstrap.Modal(this.$refs.settingsModal);
    this.$refs.settingsModal.addEventListener("hidden.bs.modal", () => this._settingsOpener?.focus());
    this.bsToast = new bootstrap.Toast(this.$refs.toast, { delay: 2600 });
    // 弹窗关闭后把焦点还给触发按钮；打开后自动聚焦首个表单控件（焦点捕获由 Bootstrap 负责）。
    this.$refs.modal.addEventListener("hidden.bs.modal", () => this._modalOpener?.focus());
    this.$refs.modal.addEventListener("shown.bs.modal", () => {
      this.$refs.modal.querySelector(".modal-body input, .modal-body textarea, .modal-body select")?.focus();
    });
    // 跟随系统配色，直到用户首次手动切换（手动切换会写入 localStorage 并永久退出跟随）。
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onMq = (e) => {
      let stored = null;
      try { stored = localStorage.getItem("ccb-theme"); } catch (_) { /* 忽略 */ }
      if (stored) return;
      this.applyTheme(e.matches ? "light" : "dark", false);
    };
    mq.addEventListener ? mq.addEventListener("change", onMq) : mq.addListener(onMq);
    this.connect();
  },
}).mount("#app");
