// Claude Chat Base 界面 —— 通过 WebSocket 连接服务端，渲染实时群聊，
// 并通过 REST 接口驱动房间 / 智能体的各项操作。

const state = {
  agents: {},        // id -> 智能体
  rooms: {},         // id -> 房间
  messages: {},      // room_id -> [消息]
  server: {},
  currentRoomId: null,
};

const PALETTE = [
  "#6366f1", "#ec4899", "#10b981", "#f59e0b", "#06b6d4",
  "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6",
];

// ---------- 小工具 ----------
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};
// 头像缩写：中文等宽字符取首字，拉丁字母取前两位。
const initials = (name) => {
  const s = (name || "?").trim();
  if (!s) return "?";
  return s.codePointAt(0) > 0x2e7f ? Array.from(s)[0] : s.slice(0, 2).toUpperCase();
};
const fmtTime = (ts) =>
  new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });

let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2600);
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    toast(`错误：${text}`);
    throw new Error(text);
  }
  return res.status === 204 ? null : res.json();
}

// ---------- WebSocket ----------
let ws;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onclose = () => {
    toast("连接断开 —— 正在重连…");
    setTimeout(connect, 1200);
  };
  ws.onerror = () => ws.close();
}

const messageEls = new Map(); // message_id -> DOM 节点

function handleEvent(ev) {
  switch (ev.type) {
    case "snapshot": return applySnapshot(ev);
    case "agent_added":
    case "agent_updated": state.agents[ev.agent.id] = ev.agent; renderAgents(); renderRooms(); break;
    case "agent_removed": delete state.agents[ev.agent_id]; renderAgents(); break;
    case "agent_status":
      if (state.agents[ev.agent_id]) state.agents[ev.agent_id].status = ev.status;
      renderAgents();
      break;
    case "room_added":
    case "room_updated":
      state.rooms[ev.room.id] = ev.room;
      if (!state.currentRoomId) selectRoom(ev.room.id);
      renderRooms(); renderHeader(); renderAgents();
      break;
    case "room_status":
      if (state.rooms[ev.room_id]) {
        state.rooms[ev.room_id].status = ev.status;
        state.rooms[ev.room_id].turn = ev.turn;
      }
      renderRooms(); renderHeader();
      break;
    case "room_reset":
      state.messages[ev.room_id] = [];
      if (ev.room_id === state.currentRoomId) renderTranscript();
      break;
    case "message": addMessage(ev.message, false); break;
    case "message_start": addMessage(ev.message, true); break;
    case "message_delta": appendDelta(ev.message_id, ev.delta); break;
    case "message_end": endMessage(ev.message); break;
  }
}

function applySnapshot(snap) {
  state.agents = Object.fromEntries(snap.agents.map((a) => [a.id, a]));
  state.rooms = Object.fromEntries(snap.rooms.map((r) => [r.id, r]));
  state.messages = snap.messages || {};
  state.server = snap.server || {};
  if (!state.currentRoomId || !state.rooms[state.currentRoomId]) {
    state.currentRoomId = snap.rooms[0]?.id || null;
  }
  renderAll();
}

// ---------- 消息渲染 ----------
function nearBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 120;
}

function addMessage(msg, streaming) {
  (state.messages[msg.room_id] ||= []).push(msg);
  if (msg.room_id !== state.currentRoomId) return;
  const transcript = $("#transcript");
  const stick = nearBottom(transcript);
  $("#empty-state")?.remove();
  const node = renderMessage(msg, streaming);
  transcript.appendChild(node);
  messageEls.set(msg.id, node);
  if (stick) transcript.scrollTop = transcript.scrollHeight;
}

function appendDelta(id, delta) {
  const node = messageEls.get(id);
  if (!node) return;
  const textEl = node.querySelector(".msg__text");
  const transcript = $("#transcript");
  const stick = nearBottom(transcript);
  textEl.textContent += delta;
  // 同步本地副本，便于切换房间时仍能正确渲染。
  const list = state.messages[state.currentRoomId] || [];
  const m = list.find((x) => x.id === id);
  if (m) m.content = textEl.textContent;
  if (stick) transcript.scrollTop = transcript.scrollHeight;
}

function endMessage(msg) {
  const list = state.messages[msg.room_id] || [];
  const m = list.find((x) => x.id === msg.id);
  if (m) Object.assign(m, msg);
  const node = messageEls.get(msg.id);
  if (node) {
    node.classList.remove("streaming");
    node.querySelector(".msg__text").textContent = msg.content;
  }
}

function renderMessage(msg, streaming) {
  if (msg.role === "system") {
    const wrap = el("div", "msg msg--system");
    wrap.appendChild(el("div", "msg__text", msg.content));
    return wrap;
  }
  const wrap = el("div", `msg msg--${msg.role}${streaming ? " streaming" : ""}`);
  const color = msg.color || "#94a3b8";
  const avatar = el("div", "avatar", initials(msg.sender_name));
  avatar.style.background = color;
  const body = el("div", "msg__body");
  const head = el("div", "msg__head");
  const name = el("span", "msg__name", msg.sender_name);
  name.style.color = color;
  head.appendChild(name);
  if (msg.meta?.model) head.appendChild(el("span", "msg__model", msg.meta.model));
  head.appendChild(el("span", "msg__time", fmtTime(msg.ts)));
  body.appendChild(head);
  body.appendChild(el("div", "msg__text", msg.content));
  wrap.appendChild(avatar);
  wrap.appendChild(body);
  return wrap;
}

function renderTranscript() {
  const transcript = $("#transcript");
  transcript.innerHTML = "";
  messageEls.clear();
  const msgs = state.messages[state.currentRoomId] || [];
  if (!msgs.length) {
    const empty = el("div", "empty-state");
    empty.id = "empty-state";
    empty.innerHTML =
      '<div class="empty-state__icon">💬</div><p>点击<strong>开始</strong>观看智能体对话，或直接输入一句话来引出话题。</p>';
    transcript.appendChild(empty);
    return;
  }
  for (const m of msgs) {
    const node = renderMessage(m, false);
    transcript.appendChild(node);
    messageEls.set(m.id, node);
  }
  transcript.scrollTop = transcript.scrollHeight;
}

// ---------- 房间 ----------
function renderRooms() {
  const list = $("#room-list");
  list.innerHTML = "";
  for (const room of Object.values(state.rooms)) {
    const li = el("li", "room-item" + (room.id === state.currentRoomId ? " active" : ""));
    li.appendChild(el("div", "room-item__name", room.name));
    const meta = el("div", "room-item__meta");
    const dot = el("span", "dot " + room.status);
    meta.appendChild(dot);
    meta.appendChild(el("span", null, `${statusText(room.status)} · ${room.agent_ids.length} 名成员`));
    li.appendChild(meta);
    li.onclick = () => selectRoom(room.id);
    list.appendChild(li);
  }
}

function statusText(s) {
  return { idle: "空闲", running: "进行中", paused: "已暂停" }[s] || s;
}

function selectRoom(id) {
  state.currentRoomId = id;
  renderRooms();
  renderHeader();
  renderTranscript();
  renderAgents();
}

// ---------- 顶栏 / 控制 ----------
function renderHeader() {
  const room = state.rooms[state.currentRoomId];
  $("#room-name").textContent = room ? room.name : "未选择房间";
  $("#room-topic").textContent = room ? room.topic : "";
  const controls = $("#chat-controls");
  controls.innerHTML = "";
  if (!room) return;

  controls.appendChild(
    el("span", "turn-counter", `第 ${room.turn}/${room.max_turns} 轮`)
  );

  const running = room.status === "running";
  const paused = room.status === "paused";

  if (!running) {
    controls.appendChild(button(paused ? "继续" : "开始", "btn--primary", () =>
      api("POST", `/api/rooms/${room.id}/start`)));
  } else {
    controls.appendChild(button("暂停", "", () => api("POST", `/api/rooms/${room.id}/pause`)));
  }
  if (running || paused) {
    controls.appendChild(button("停止", "btn--ghost", () => api("POST", `/api/rooms/${room.id}/stop`)));
  }
  controls.appendChild(button("重置", "btn--ghost btn--danger", () => api("POST", `/api/rooms/${room.id}/reset`)));
  controls.appendChild(button("编辑", "btn--ghost", () => editRoom(room)));
}

function button(label, cls, onClick) {
  const b = el("button", `btn ${cls}`.trim(), label);
  b.onclick = onClick;
  return b;
}

// ---------- 智能体 ----------
function renderAgents() {
  const room = state.rooms[state.currentRoomId];
  const list = $("#agent-list");
  list.innerHTML = "";
  if (!room) return;
  for (const aid of room.agent_ids) {
    const a = state.agents[aid];
    if (!a) continue;
    const isPeer = a.kind === "peer";
    const li = el("li", "agent-item" + (a.enabled ? "" : " disabled"));
    const sw = el("span", "swatch");
    sw.style.background = a.color;
    li.appendChild(sw);

    const info = el("div", "agent-item__info");
    const nameRow = el("div", "agent-item__name");
    nameRow.appendChild(el("span", null, a.name));
    if (isPeer) {
      const dot = el("span", "dot " + (a.online ? "running" : ""));
      dot.title = a.online ? "在线" : "离线";
      nameRow.appendChild(dot);
    }
    info.appendChild(nameRow);
    info.appendChild(el("div", `agent-item__status ${a.status}`, statusLabel(a)));
    if (isPeer && a.repo_path) {
      info.appendChild(el("div", "agent-item__path", a.repo_path));
    }
    li.appendChild(info);

    if (isPeer) {
      const join = el("button", "tiny-btn", "⧉");
      join.title = "复制接入命令";
      join.onclick = () => copyJoinCommand(a, room);
      li.appendChild(join);
    } else {
      const toggle = el("button", "tiny-btn", a.enabled ? "🔵" : "⚪");
      toggle.title = a.enabled ? "静音" : "取消静音";
      toggle.onclick = () => api("PATCH", `/api/agents/${a.id}`, { enabled: !a.enabled });
      li.appendChild(toggle);
    }
    const edit = el("button", "tiny-btn", "✎");
    edit.title = "编辑";
    edit.onclick = () => editAgent(a);
    li.appendChild(edit);

    const rm = el("button", "tiny-btn", "✕");
    rm.title = "移出房间";
    rm.onclick = () => api("DELETE", `/api/rooms/${room.id}/agents/${a.id}`);
    li.appendChild(rm);
    list.appendChild(li);
  }
  renderAddExisting();
}

function statusLabel(a) {
  if (a.kind === "peer") {
    const role = a.role ? a.role + " · " : "";
    return role + (a.online ? "在线" : "离线（等待 Claude Code 接入）");
  }
  if (!a.enabled) return "已静音";
  if (a.status === "thinking") return "思考中…";
  if (a.status === "speaking") return "发言中…";
  return "空闲";
}

async function copyJoinCommand(a, room) {
  const text =
    `# 在「${a.role || a.name}」仓库目录启动 Claude Code，并注册一次 MCP（如未注册过）：\n` +
    `claude mcp add --transport stdio ccb -- uv run --project <claude-chat-base 路径> ccb-mcp\n\n` +
    `# 然后对该会话说（或让它执行）：\n` +
    `加入 CCB 房间「${room.name}」，作为「${a.name}」，角色 ${a.role || a.name}` +
    `${a.repo_path ? `，仓库路径 ${a.repo_path}` : ""}。\n` +
    `请调用 join_room("${room.name}", "${a.name}", "${a.role || ""}", "${a.repo_path || ""}")，\n` +
    `随后用 wait_for_messages 持续跟进，被点名或有相关变更时用 send_message 回应。`;
  try {
    await navigator.clipboard.writeText(text);
    toast(`已复制「${a.name}」的接入命令`);
  } catch {
    toast("复制失败，请手动复制");
    console.log(text);
  }
}

function renderAddExisting() {
  const room = state.rooms[state.currentRoomId];
  const wrap = $("#add-existing");
  wrap.innerHTML = "";
  if (!room) return;
  const available = Object.values(state.agents).filter((a) => !room.agent_ids.includes(a.id));
  const select = el("select");
  select.appendChild(el("option", null, available.length ? "添加一个智能体…" : "没有可添加的智能体"));
  for (const a of available) {
    const opt = el("option", null, a.name);
    opt.value = a.id;
    select.appendChild(opt);
  }
  select.disabled = !available.length;
  select.onchange = () => {
    if (select.value) api("POST", `/api/rooms/${room.id}/agents/${select.value}`);
  };
  wrap.appendChild(select);
}

// ---------- 服务端信息卡 ----------
function renderServerCard() {
  const s = state.server;
  const card = $("#server-card");
  const provider = s.provider || "mock";
  card.innerHTML = "";
  const line1 = el("div");
  line1.innerHTML = `提供方 <span class="badge ${provider}">${provider}</span>`;
  card.appendChild(line1);
  card.appendChild(el("div", null, `模型：${s.default_model || "—"}`));
  if (!s.has_api_key) {
    card.appendChild(el("div", null, "提示：设置 CCB_ANTHROPIC_API_KEY 可启用真实智能体。"));
  }
}

function renderAll() {
  renderRooms();
  renderHeader();
  renderTranscript();
  renderAgents();
  renderServerCard();
}

// ---------- 输入框 ----------
function setupComposer() {
  const input = $("#composer-input");
  const send = () => {
    const text = input.value.trim();
    if (!text || !state.currentRoomId) return;
    api("POST", `/api/rooms/${state.currentRoomId}/messages`, { content: text });
    input.value = "";
    input.style.height = "auto";
  };
  $("#send-btn").onclick = send;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 140) + "px";
  });
}

// ---------- 弹窗 ----------
function openModal(title, fields, onSave) {
  $("#modal-title").textContent = title;
  const body = $("#modal-body");
  body.innerHTML = "";
  const refs = {};
  for (const f of fields) {
    const field = el("div", "field");
    field.appendChild(el("label", null, f.label));
    let control;
    if (f.type === "textarea") {
      control = el("textarea");
      control.value = f.value || "";
    } else if (f.type === "select") {
      control = el("select");
      for (const opt of f.options) {
        const o = el("option", null, opt.label);
        o.value = opt.value;
        if (opt.value === f.value) o.selected = true;
        control.appendChild(o);
      }
    } else if (f.type === "color") {
      control = el("div", "swatch-row");
      let chosen = f.value || PALETTE[0];
      for (const c of PALETTE) {
        const sw = el("div", "swatch-pick" + (c === chosen ? " sel" : ""));
        sw.style.background = c;
        sw.onclick = () => {
          chosen = c;
          [...control.children].forEach((x) => x.classList.remove("sel"));
          sw.classList.add("sel");
        };
        control.appendChild(sw);
      }
      refs[f.key] = () => chosen;
      field.appendChild(control);
      body.appendChild(field);
      continue;
    } else {
      control = el("input");
      control.type = f.type || "text";
      control.value = f.value ?? "";
    }
    refs[f.key] = () => control.value;
    field.appendChild(control);
    body.appendChild(field);
  }
  const backdrop = $("#modal-backdrop");
  backdrop.hidden = false;
  $("#modal-ok").onclick = async () => {
    const values = {};
    for (const k in refs) values[k] = refs[k]();
    await onSave(values);
    backdrop.hidden = true;
  };
}
function closeModal() { $("#modal-backdrop").hidden = true; }

const STRATEGY_OPTIONS = [
  { label: "主持人（由模型挑选发言者）", value: "director" },
  { label: "轮流发言", value: "round_robin" },
];

// ---------- 弹窗操作 ----------
function newRoom() {
  const agentOpts = Object.values(state.agents).map((a) => ({ label: a.name, value: a.id }));
  openModal("新建房间", [
    { key: "name", label: "房间名称", value: "" },
    { key: "topic", label: "话题 / 目标", type: "textarea", value: "" },
    { key: "strategy", label: "发言方式", type: "select", value: "director", options: STRATEGY_OPTIONS },
    { key: "max_turns", label: "最大轮数", type: "number", value: 18 },
  ], async (v) => {
    const room = await api("POST", "/api/rooms", {
      name: v.name || "新房间",
      topic: v.topic,
      strategy: v.strategy,
      max_turns: parseInt(v.max_turns) || 18,
      agent_ids: agentOpts.map((o) => o.value),
    });
    selectRoom(room.id);
  });
}

function editRoom(room) {
  openModal("编辑房间", [
    { key: "name", label: "房间名称", value: room.name },
    { key: "topic", label: "话题 / 目标", type: "textarea", value: room.topic },
    { key: "strategy", label: "发言方式", type: "select", value: room.strategy, options: STRATEGY_OPTIONS },
    { key: "max_turns", label: "最大轮数", type: "number", value: room.max_turns },
    { key: "turn_delay", label: "每轮间隔（秒）", type: "number", value: room.turn_delay },
  ], (v) => api("PATCH", `/api/rooms/${room.id}`, {
    name: v.name, topic: v.topic, strategy: v.strategy,
    max_turns: parseInt(v.max_turns) || room.max_turns,
    turn_delay: parseFloat(v.turn_delay) || room.turn_delay,
  }));
}

const KIND_OPTIONS = [
  { label: "仓库 peer（接入真实 Claude Code）", value: "peer" },
  { label: "AI 智能体（API 自动发言）", value: "ai" },
];

function newAgent() {
  openModal("新增参与者", [
    { key: "kind", label: "类型", type: "select", value: "peer", options: KIND_OPTIONS },
    { key: "name", label: "名称", value: "" },
    { key: "role", label: "仓库角色（如 后端 / web端，可选）", value: "" },
    { key: "repo_path", label: "仓库本地路径（可选）", value: "" },
    { key: "persona", label: "人设 / 仓库上下文（系统提示）", type: "textarea", value: "" },
    { key: "color", label: "颜色", type: "color", value: PALETTE[Object.keys(state.agents).length % PALETTE.length] },
  ], async (v) => {
    const agent = await api("POST", "/api/agents", {
      name: v.name || (v.kind === "peer" ? "仓库" : "智能体"),
      kind: v.kind, role: v.role, repo_path: v.repo_path,
      persona: v.persona, color: v.color,
    });
    if (state.currentRoomId) {
      await api("POST", `/api/rooms/${state.currentRoomId}/agents/${agent.id}`);
    }
  });
}

function editAgent(a) {
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
  openModal("编辑参与者", fields, (v) => api("PATCH", `/api/agents/${a.id}`, {
    name: v.name, role: v.role, repo_path: v.repo_path, persona: v.persona,
    color: v.color,
    ...(a.kind === "ai" && v.temperature != null ? { temperature: parseFloat(v.temperature) } : {}),
  }));
}

// ---------- 启动 ----------
function main() {
  $("#new-room-btn").onclick = newRoom;
  $("#add-agent-btn").onclick = newAgent;
  $("#modal-close").onclick = closeModal;
  $("#modal-cancel").onclick = closeModal;
  $("#modal-backdrop").onclick = (e) => { if (e.target.id === "modal-backdrop") closeModal(); };
  setupComposer();
  connect();
}

main();
