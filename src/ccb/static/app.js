// Claude Chat Base 界面 —— Vue 3（本地 vendor，免构建）+ Bootstrap 5 组件。
// 通过 WebSocket 接收事件、驱动响应式状态；通过 REST 接口执行操作。
const { createApp } = Vue;

const PALETTE = [
  "#6366f1", "#ec4899", "#10b981", "#f59e0b", "#06b6d4",
  "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6",
];
const STRATEGY_OPTIONS = [
  { label: "主持人（由模型挑选发言者）", value: "director" },
  { label: "轮流发言", value: "round_robin" },
];
const KIND_OPTIONS = [
  { label: "仓库 peer（接入真实 Claude Code）", value: "peer" },
  { label: "AI 智能体（API 自动发言）", value: "ai" },
];

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
      modal: { title: "", fields: [], values: {}, onSave: null },
    };
  },

  computed: {
    roomList() { return Object.values(this.rooms); },
    currentRoom() { return this.rooms[this.currentRoomId] || null; },
    currentMessages() { return this.messages[this.currentRoomId] || []; },
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
      ws.onmessage = (e) => this.handleEvent(JSON.parse(e.data));
      ws.onclose = () => { this.toast("连接断开 —— 正在重连…"); setTimeout(() => this.connect(), 1200); };
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
      this.messages[m.room_id].push(m);
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
    },
    onScroll() {
      const t = this.$refs.transcript;
      if (t) this.stick = t.scrollHeight - t.scrollTop - t.clientHeight < 120;
    },
    scrollToBottom() {
      const t = this.$refs.transcript;
      if (t) t.scrollTop = t.scrollHeight;
    },

    // ----- 主题 -----
    selectRoom(id) {
      this.currentRoomId = id;
      this.stick = true;
      this.$nextTick(() => this.scrollToBottom());
    },
    async roomAction(action) {
      if (this.currentRoom) await this.api("POST", `/api/rooms/${this.currentRoom.id}/${action}`);
    },

    // ----- 输入框 -----
    sendMessage() {
      const text = this.draft.trim();
      if (!text || !this.currentRoomId) return;
      this.api("POST", `/api/rooms/${this.currentRoomId}/messages`, { content: text }).catch(() => {});
      this.draft = "";
      const el = this.$refs.composer;
      if (el) el.style.height = "auto";
    },
    autoGrow(e) {
      const el = e.target;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 140) + "px";
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
      this.bsModal.show();
    },
    hideModal() { this.bsModal.hide(); },
    async saveModal() {
      if (!this.modal.onSave) return this.hideModal();
      try { await this.modal.onSave(this.modal.values); this.bsModal.hide(); }
      catch { /* api() 已弹出错误提示，弹窗保持打开 */ }
    },
    newRoom() {
      this.openModal("新建主题", [
        { key: "name", label: "主题名称", value: "" },
        { key: "topic", label: "话题 / 目标", type: "textarea", value: "" },
        { key: "strategy", label: "发言方式", type: "select", value: "director", options: STRATEGY_OPTIONS },
        { key: "max_turns", label: "最大轮数", type: "number", value: 18 },
      ], async (v) => {
        // 新主题默认不预先拉入任何成员，由你按需用「实例」面板或「添加已有」加入。
        const room = await this.api("POST", "/api/rooms", {
          name: v.name || "新主题", topic: v.topic, strategy: v.strategy,
          max_turns: parseInt(v.max_turns) || 18, agent_ids: [],
        });
        this.selectRoom(room.id);
      });
    },
    editRoom(room) {
      this.openModal("编辑主题", [
        { key: "name", label: "主题名称", value: room.name },
        { key: "topic", label: "话题 / 目标", type: "textarea", value: room.topic },
        { key: "strategy", label: "发言方式", type: "select", value: room.strategy, options: STRATEGY_OPTIONS },
        { key: "max_turns", label: "最大轮数", type: "number", value: room.max_turns },
        { key: "turn_delay", label: "每轮间隔（秒）", type: "number", value: room.turn_delay },
      ], (v) => this.api("PATCH", `/api/rooms/${room.id}`, {
        name: v.name, topic: v.topic, strategy: v.strategy,
        max_turns: parseInt(v.max_turns) || room.max_turns,
        turn_delay: parseFloat(v.turn_delay) || room.turn_delay,
      }));
    },
    newAgent() {
      this.openModal("新增参与者", [
        { key: "kind", label: "类型", type: "select", value: "peer", options: KIND_OPTIONS },
        { key: "name", label: "名称", value: "" },
        { key: "role", label: "仓库角色（如 后端 / web端，可选）", value: "" },
        { key: "repo_path", label: "仓库本地路径（可选）", value: "" },
        { key: "persona", label: "人设 / 仓库上下文（系统提示）", type: "textarea", value: "" },
        { key: "color", label: "颜色", type: "color", value: PALETTE[Object.keys(this.agents).length % PALETTE.length] },
      ], async (v) => {
        const agent = await this.api("POST", "/api/agents", {
          name: v.name || (v.kind === "peer" ? "仓库" : "智能体"),
          kind: v.kind, role: v.role, repo_path: v.repo_path, persona: v.persona, color: v.color,
        });
        if (this.currentRoomId) await this.api("POST", `/api/rooms/${this.currentRoomId}/agents/${agent.id}`);
      });
    },
    editAgent(a) {
      const fields = [
        { key: "name", label: "名称", value: a.name },
        { key: "role", label: "仓库角色（如 后端 / web端，可选）", value: a.role || "" },
        { key: "repo_path", label: "仓库本地路径（可选）", value: a.repo_path || "" },
        { key: "persona", label: "人设 / 仓库上下文（系统提示）", type: "textarea", value: a.persona },
        { key: "color", label: "颜色", type: "color", value: a.color },
      ];
      if (a.kind === "ai") {
        fields.splice(4, 0, { key: "temperature", label: "温度", type: "number", value: a.temperature });
      }
      this.openModal("编辑参与者", fields, (v) => this.api("PATCH", `/api/agents/${a.id}`, {
        name: v.name, role: v.role, repo_path: v.repo_path, persona: v.persona, color: v.color,
        ...(a.kind === "ai" && v.temperature != null ? { temperature: parseFloat(v.temperature) } : {}),
      }));
    },

    toast(msg) { this.toastMsg = msg; this.bsToast.show(); },
  },

  updated() {
    if (this.stick) this.scrollToBottom();
  },
  mounted() {
    this.bsModal = new bootstrap.Modal(this.$refs.modal);
    this.bsToast = new bootstrap.Toast(this.$refs.toast, { delay: 2600 });
    this.connect();
  },
}).mount("#app");
