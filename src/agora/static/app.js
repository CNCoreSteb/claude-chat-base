// Agora GUI — connects to the server over WebSocket, renders the live group chat,
// and drives room/agent controls through the REST API.

const state = {
  agents: {},        // id -> agent
  rooms: {},         // id -> room
  messages: {},      // room_id -> [message]
  server: {},
  currentRoomId: null,
};

const PALETTE = [
  "#6366f1", "#ec4899", "#10b981", "#f59e0b", "#06b6d4",
  "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#3b82f6",
];

// ---------- tiny helpers ----------
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};
const initials = (name) => (name || "?").trim().slice(0, 2).toUpperCase();
const fmtTime = (ts) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

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
    toast(`Error: ${text}`);
    throw new Error(text);
  }
  return res.status === 204 ? null : res.json();
}

// ---------- websocket ----------
let ws;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onclose = () => {
    toast("Disconnected — reconnecting…");
    setTimeout(connect, 1200);
  };
  ws.onerror = () => ws.close();
}

const messageEls = new Map(); // message_id -> DOM node

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

// ---------- message rendering ----------
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
  // keep our local copy in sync for room switches
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
      '<div class="empty-state__icon">💬</div><p>Press <strong>Start</strong> to watch the agents talk, or type a message to kick things off.</p>';
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

// ---------- rooms ----------
function renderRooms() {
  const list = $("#room-list");
  list.innerHTML = "";
  for (const room of Object.values(state.rooms)) {
    const li = el("li", "room-item" + (room.id === state.currentRoomId ? " active" : ""));
    li.appendChild(el("div", "room-item__name", room.name));
    const meta = el("div", "room-item__meta");
    const dot = el("span", "dot " + room.status);
    meta.appendChild(dot);
    meta.appendChild(el("span", null, `${room.status} · ${room.agent_ids.length} agents`));
    li.appendChild(meta);
    li.onclick = () => selectRoom(room.id);
    list.appendChild(li);
  }
}

function selectRoom(id) {
  state.currentRoomId = id;
  renderRooms();
  renderHeader();
  renderTranscript();
  renderAgents();
}

// ---------- header / controls ----------
function renderHeader() {
  const room = state.rooms[state.currentRoomId];
  $("#room-name").textContent = room ? room.name : "No room selected";
  $("#room-topic").textContent = room ? room.topic : "";
  const controls = $("#chat-controls");
  controls.innerHTML = "";
  if (!room) return;

  controls.appendChild(
    el("span", "turn-counter", `turn ${room.turn}/${room.max_turns}`)
  );

  const running = room.status === "running";
  const paused = room.status === "paused";

  if (!running) {
    controls.appendChild(button(paused ? "Resume" : "Start", "btn--primary", () =>
      api("POST", `/api/rooms/${room.id}/start`)));
  } else {
    controls.appendChild(button("Pause", "", () => api("POST", `/api/rooms/${room.id}/pause`)));
  }
  if (running || paused) {
    controls.appendChild(button("Stop", "btn--ghost", () => api("POST", `/api/rooms/${room.id}/stop`)));
  }
  controls.appendChild(button("Reset", "btn--ghost btn--danger", () => api("POST", `/api/rooms/${room.id}/reset`)));
  controls.appendChild(button("Edit", "btn--ghost", () => editRoom(room)));
}

function button(label, cls, onClick) {
  const b = el("button", `btn ${cls}`.trim(), label);
  b.onclick = onClick;
  return b;
}

// ---------- agents ----------
function renderAgents() {
  const room = state.rooms[state.currentRoomId];
  const list = $("#agent-list");
  list.innerHTML = "";
  if (!room) return;
  for (const aid of room.agent_ids) {
    const a = state.agents[aid];
    if (!a) continue;
    const li = el("li", "agent-item" + (a.enabled ? "" : " disabled"));
    const sw = el("span", "swatch");
    sw.style.background = a.color;
    li.appendChild(sw);
    const info = el("div", "agent-item__info");
    const nameRow = el("div", "agent-item__name");
    nameRow.appendChild(el("span", null, a.name));
    if (a.kind === "peer") nameRow.appendChild(el("span", "badge", "peer"));
    info.appendChild(nameRow);
    info.appendChild(el("div", `agent-item__status ${a.status}`, statusLabel(a)));
    li.appendChild(info);

    if (a.kind === "ai") {
      const toggle = el("button", "tiny-btn", a.enabled ? "🔵" : "⚪");
      toggle.title = a.enabled ? "Mute" : "Unmute";
      toggle.onclick = () => api("PATCH", `/api/agents/${a.id}`, { enabled: !a.enabled });
      li.appendChild(toggle);
      const edit = el("button", "tiny-btn", "✎");
      edit.title = "Edit";
      edit.onclick = () => editAgent(a);
      li.appendChild(edit);
    }
    const rm = el("button", "tiny-btn", "✕");
    rm.title = "Remove from room";
    rm.onclick = () => api("DELETE", `/api/rooms/${room.id}/agents/${a.id}`);
    li.appendChild(rm);
    list.appendChild(li);
  }
  renderAddExisting();
}

function statusLabel(a) {
  if (!a.enabled) return "muted";
  if (a.status === "thinking") return "thinking…";
  if (a.status === "speaking") return "speaking…";
  return a.kind === "peer" ? "peer (external)" : "idle";
}

function renderAddExisting() {
  const room = state.rooms[state.currentRoomId];
  const wrap = $("#add-existing");
  wrap.innerHTML = "";
  if (!room) return;
  const available = Object.values(state.agents).filter((a) => !room.agent_ids.includes(a.id));
  const select = el("select");
  select.appendChild(el("option", null, available.length ? "Add an agent…" : "No spare agents"));
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

// ---------- server card ----------
function renderServerCard() {
  const s = state.server;
  const card = $("#server-card");
  const provider = s.provider || "mock";
  card.innerHTML = "";
  const line1 = el("div");
  line1.innerHTML = `Provider <span class="badge ${provider}">${provider}</span>`;
  card.appendChild(line1);
  card.appendChild(el("div", null, `Model: ${s.default_model || "—"}`));
  if (!s.has_api_key) {
    card.appendChild(el("div", null, "Tip: set AGORA_ANTHROPIC_API_KEY for real agents."));
  }
}

function renderAll() {
  renderRooms();
  renderHeader();
  renderTranscript();
  renderAgents();
  renderServerCard();
}

// ---------- composer ----------
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

// ---------- modal ----------
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

// ---------- modal actions ----------
function newRoom() {
  const agentOpts = Object.values(state.agents).map((a) => ({ label: a.name, value: a.id }));
  openModal("New room", [
    { key: "name", label: "Room name", value: "" },
    { key: "topic", label: "Topic / goal", type: "textarea", value: "" },
    { key: "strategy", label: "Turn-taking", type: "select", value: "director",
      options: [{ label: "Director (LLM picks speaker)", value: "director" },
                { label: "Round robin", value: "round_robin" }] },
    { key: "max_turns", label: "Max turns", type: "number", value: 18 },
  ], async (v) => {
    const room = await api("POST", "/api/rooms", {
      name: v.name || "New room",
      topic: v.topic,
      strategy: v.strategy,
      max_turns: parseInt(v.max_turns) || 18,
      agent_ids: agentOpts.map((o) => o.value),
    });
    selectRoom(room.id);
  });
}

function editRoom(room) {
  openModal("Edit room", [
    { key: "name", label: "Room name", value: room.name },
    { key: "topic", label: "Topic / goal", type: "textarea", value: room.topic },
    { key: "strategy", label: "Turn-taking", type: "select", value: room.strategy,
      options: [{ label: "Director (LLM picks speaker)", value: "director" },
                { label: "Round robin", value: "round_robin" }] },
    { key: "max_turns", label: "Max turns", type: "number", value: room.max_turns },
    { key: "turn_delay", label: "Delay between turns (s)", type: "number", value: room.turn_delay },
  ], (v) => api("PATCH", `/api/rooms/${room.id}`, {
    name: v.name, topic: v.topic, strategy: v.strategy,
    max_turns: parseInt(v.max_turns) || room.max_turns,
    turn_delay: parseFloat(v.turn_delay) || room.turn_delay,
  }));
}

function newAgent() {
  openModal("New agent", [
    { key: "name", label: "Name", value: "" },
    { key: "persona", label: "Persona / role (system prompt)", type: "textarea", value: "" },
    { key: "color", label: "Color", type: "color", value: PALETTE[Object.keys(state.agents).length % PALETTE.length] },
  ], async (v) => {
    const agent = await api("POST", "/api/agents", {
      name: v.name || "Agent", persona: v.persona, color: v.color,
    });
    if (state.currentRoomId) {
      await api("POST", `/api/rooms/${state.currentRoomId}/agents/${agent.id}`);
    }
  });
}

function editAgent(a) {
  openModal("Edit agent", [
    { key: "name", label: "Name", value: a.name },
    { key: "persona", label: "Persona / role (system prompt)", type: "textarea", value: a.persona },
    { key: "temperature", label: "Temperature", type: "number", value: a.temperature },
    { key: "color", label: "Color", type: "color", value: a.color },
  ], (v) => api("PATCH", `/api/agents/${a.id}`, {
    name: v.name, persona: v.persona,
    temperature: parseFloat(v.temperature), color: v.color,
  }));
}

// ---------- boot ----------
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
