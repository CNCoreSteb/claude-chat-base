// Claude Chat Base 界面 —— Vue 3（本地 vendor，免构建）+ Bootstrap 5 组件。
// 通过 WebSocket 接收事件、驱动响应式状态；通过 REST 接口执行操作。
const { createApp } = Vue;

const PALETTE = [
  "#6366f1", "#ec4899", "#10b981", "#f59e0b", "#06b6d4",
  "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6",
];
// —— 以下为 AI 智能体相关选项；AI 自动对话已暂时停用，本项目当前专注于多 Claude Code 协作。——
// const STRATEGY_OPTIONS = [
//   { label: "主持人（由模型挑选发言者）", value: "director" },
//   { label: "轮流发言", value: "round_robin" },
// ];
// const KIND_OPTIONS = [
//   { label: "仓库 peer（接入真实 Claude Code）", value: "peer" },
//   { label: "AI 智能体（API 自动发言）", value: "ai" },
// ];

createApp({
  data() {
    return {
      agents: {},        // id -> 智能体
      rooms: {},         // id -> 主题
      messages: {},      // room_id -> [消息]
      server: {},
      currentRoomId: null,
      draft: "",
      stick: true,       // 是否贴着底部（决定流式时是否自动滚动）
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
    };
  },

  computed: {
    roomList() { return Object.values(this.rooms); },
    currentRoom() { return this.rooms[this.currentRoomId] || null; },
    currentMessages() { return this.messages[this.currentRoomId] || []; },
    // 当前主题里被引用回复过的消息 id 集合。
    repliedToIds() {
      const s = new Set();
      for (const m of this.currentMessages) {
        const rid = m.meta && m.meta.reply_to;
        if (rid) s.add(rid);
      }
      return s;
    },
    // 被视为"已回复"的提问：被引用回复过，或其后本主题出现过任何人类发言（ask 在收到
    // 下一条 human 消息时即返回，普通直接回答也应让"❓ 等你回答"翻成"✅ 已回复"）。
    answeredQuestionIds() {
      const s = new Set(this.repliedToIds);
      let lastHumanTs = -Infinity;
      for (const m of this.currentMessages) {
        if (m.role === "human") lastHumanTs = Math.max(lastHumanTs, m.ts);
      }
      for (const m of this.currentMessages) {
        if (m.meta && m.meta.is_question && m.ts < lastHumanTs) s.add(m.id);
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
    statusText(s) { return { idle: "空闲", running: "进行中", paused: "已暂停" }[s] || s; },
    statusLabel(a) {
      if (a.kind === "peer") {
        return (a.role ? a.role + " · " : "") + (a.online ? "在线" : "离线（等待 Claude Code 接入）");
      }
      if (!a.enabled) return "已静音";
      if (a.status === "thinking") return "思考中…";
      if (a.status === "speaking") return "发言中…";
      return "空闲";
    },
    statusClass(a) {
      if (a.kind !== "peer") {
        if (a.status === "thinking") return "text-warning";
        if (a.status === "speaking") return "text-success";
      }
      return "text-secondary";
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
        case "agent_status": { const a = this.agents[ev.agent_id]; if (a) a.status = ev.status; break; }
        case "room_added":
        case "room_updated":
          this.rooms[ev.room.id] = ev.room;
          if (!this.currentRoomId) this.selectRoom(ev.room.id);
          break;
        case "room_status": { const r = this.rooms[ev.room_id]; if (r) { r.status = ev.status; r.turn = ev.turn; } break; }
        case "room_reset": this.messages[ev.room_id] = []; break;
        case "room_removed": {
          delete this.rooms[ev.room_id];
          delete this.messages[ev.room_id];
          if (this.currentRoomId === ev.room_id) {
            this.currentRoomId = Object.keys(this.rooms)[0] || null;
            this.replyTo = null;
            this.closeMention();
          }
          break;
        }
        case "message": this.addMessage(ev.message, false); break;
        case "message_start": this.addMessage(ev.message, true); break;
        case "message_delta": this.appendDelta(ev.message_id, ev.delta); break;
        case "message_end": this.endMessage(ev.message); break;
      }
    },
    applySnapshot(snap) {
      this.agents = Object.fromEntries(snap.agents.map((a) => [a.id, a]));
      this.rooms = Object.fromEntries(snap.rooms.map((r) => [r.id, r]));
      this.messages = snap.messages || {};
      this.server = snap.server || {};
      if (!this.currentRoomId || !this.rooms[this.currentRoomId]) {
        this.currentRoomId = snap.rooms[0]?.id || null;
      }
      this.stick = true;
      this.$nextTick(() => this.scrollToBottom());
    },

    // ----- 消息 -----
    addMessage(msg, streaming) {
      const m = { ...msg, streaming: !!streaming };
      if (!this.messages[m.room_id]) this.messages[m.room_id] = [];
      const list = this.messages[m.room_id];
      list.push(m);
      // 限制单主题在内存里保留的消息数，避免长会话无界增长（历史仍在服务端，刷新即重新快照）。
      if (list.length > 2000) list.splice(0, list.length - 2000);
      if (m.room_id === this.currentRoomId && this.stick) this.$nextTick(() => this.scrollToBottom());
    },
    appendDelta(id, delta) {
      for (const list of Object.values(this.messages)) {
        const m = list.find((x) => x.id === id);
        if (m) { m.content += delta; break; }
      }
      if (this.stick) this.$nextTick(() => this.scrollToBottom());
    },
    endMessage(msg) {
      const list = this.messages[msg.room_id];
      if (list) {
        const m = list.find((x) => x.id === msg.id);
        if (m) { Object.assign(m, msg); m.streaming = false; }
      }
      // 流式收尾时高度可能变化；若仍贴底则补一次精确滚动（取代已移除的全局 updated 钩子）。
      if (msg.room_id === this.currentRoomId && this.stick) {
        this.$nextTick(() => this.scrollToBottom());
      }
    },
    onScroll() {
      const t = this.$refs.transcript;
      if (t) this.stick = t.scrollHeight - t.scrollTop - t.clientHeight < 120;
    },
    scrollToBottom() {
      const t = this.$refs.transcript;
      // 用 behavior:auto 瞬时贴底——流式高频更新时若用 smooth 会持续追不上底部而抖动。
      if (t) t.scrollTo({ top: t.scrollHeight, behavior: "auto" });
    },

    // ----- 主题 -----
    selectRoom(id) {
      this.currentRoomId = id;
      this.replyTo = null;            // 切主题：清掉上个主题里选中的回复目标
      this.closeMention();
      this.stick = true;
      // 切换主题时用平滑滚动（仅此一处），保留切换的顺滑观感。
      this.$nextTick(() => {
        const t = this.$refs.transcript;
        if (t) t.scrollTo({ top: t.scrollHeight, behavior: "smooth" });
      });
    },
    async roomAction(action) {
      if (this.currentRoom) await this.api("POST", `/api/rooms/${this.currentRoom.id}/${action}`);
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
      this.api("POST", `/api/rooms/${this.currentRoomId}/messages`, body).catch(() => {});
      this.draft = "";
      this.replyTo = null;
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
      // 当前只新增「仓库 peer」槽位（AI 智能体已停用）。
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
      // AI 自动对话停用：不再编辑 温度（仅 AI 智能体相关）。
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
