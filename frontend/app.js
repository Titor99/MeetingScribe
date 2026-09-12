/* 会议实时转写 - 前端逻辑（原生 JS，全离线） */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  ws: null,
  meeting: null,          // 当前进行中会议 meta
  speakers: [],
  segments: [],           // 当前显示的转写
  autoScroll: true,
  fontLevel: 0,
  showTime: true,         // 显示时间戳
  timerInt: null,
  elapsedBase: 0,
  elapsedStart: 0,
  viewingHistory: false,
  historyId: null,        // 正在查看的历史会议 ID
  llmBaseUrl: null,       // 本地大模型地址（来自后端 state）
  voiceprints: [],        // 固定声纹库
};

/* ---------------- 显示偏好（localStorage 持久化） ---------------- */
const FONT_CLASSES = ["", "font-lg", "font-xl"];
function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem("ms_prefs") || "{}");
    if (Number.isInteger(p.fontLevel)) state.fontLevel = Math.min(2, Math.max(0, p.fontLevel));
    if (typeof p.autoScroll === "boolean") state.autoScroll = p.autoScroll;
    if (typeof p.showTime === "boolean") state.showTime = p.showTime;
  } catch (e) { /* 忽略损坏的偏好 */ }
}
function savePrefs() {
  localStorage.setItem("ms_prefs", JSON.stringify({
    fontLevel: state.fontLevel, autoScroll: state.autoScroll, showTime: state.showTime,
  }));
}
function applyPrefs() {
  $("transcript").className = "transcript " + FONT_CLASSES[state.fontLevel];
  document.body.classList.toggle("hide-time", !state.showTime);
  // 同步设置抽屉里的控件状态
  document.querySelectorAll("#set-font button").forEach(b =>
    b.classList.toggle("on", parseInt(b.dataset.v) === state.fontLevel));
  $("set-autoscroll").classList.toggle("on", state.autoScroll);
  $("set-timestamp").classList.toggle("on", state.showTime);
}

/* ---------------- WebSocket ---------------- */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => setWsStatus(true);
  ws.onclose = () => { setWsStatus(false); setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    handleMessage(msg);
  };
}

/* ---------------- 合并状态灯（顶栏） ---------------- */
const statusState = { model: "loading", ws: false, device: "", deviceAvail: "" };
function updateStatusPill() {
  const pill = $("status-pill"), txt = $("pill-text");
  const modelTxt = statusState.model === "ok" ? "模型：已就绪（本地离线）"
    : statusState.model === "err" ? "模型：加载失败" : "模型：加载中…";
  const wsTxt = statusState.ws ? "服务：已连接" : "服务：连接断开";
  const devName = statusState.device || statusState.deviceAvail;
  const devTxt = statusState.device ? "设备：" + statusState.device + "（录音中）"
    : statusState.deviceAvail ? "设备：" + statusState.deviceAvail + "（就绪）"
    : "设备：未检测到录音设备";
  pill.title = modelTxt + "\n" + wsTxt + "\n" + devTxt;
  let cls = "ok", label = "已就绪";
  if (statusState.model === "err")      { cls = "err";  label = "模型加载失败"; }
  else if (!statusState.ws)             { cls = "err";  label = "服务连接断开"; }
  else if (statusState.model !== "ok")  { cls = "warn"; label = "模型加载中…"; }
  else if (!devName)                    { cls = "warn"; label = "未检测到录音设备"; }
  pill.className = "status-pill " + cls;
  txt.textContent = label;
}

function setWsStatus(ok) {
  statusState.ws = ok;
  updateStatusPill();
}

function handleMessage(msg) {
  switch (msg.type) {
    case "hello":
    case "status":
      applyState(msg.state);
      break;
    case "voiceprints":
      state.voiceprints = msg.voiceprints || [];
      renderVoiceprints();
      break;
    case "speaker_renamed": {
      // 实时更新说话人：说话人列表 + 已上屏的所有该说话人发言（姓名/颜色/角色）
      state.speakers = msg.speakers || state.speakers;
      const spk = (state.speakers.find(s => s.id === msg.speaker_id)) || msg;
      document.querySelectorAll(`#transcript .seg[data-spk="${msg.speaker_id}"]`).forEach(el => {
        const nameEl = el.querySelector(".seg-name");
        if (nameEl) { nameEl.textContent = spk.label || msg.name; nameEl.style.color = spk.color || msg.color; }
        const avEl = el.querySelector(".seg-avatar");
        if (avEl && (spk.color || msg.color)) avEl.style.background = spk.color || msg.color;
        const roleEl = el.querySelector(".seg-role");
        if (roleEl) roleEl.textContent = spk.role ? `· ${spk.role}` : "";
      });
      break;
    }
    case "segment": {
      if (state.viewingHistory) exitHistoryView();
      hideWelcome();
      state.speakers = msg.speakers || state.speakers;
      appendSegment(msg.segment);
      break;
    }
    case "level": {
      const pct = Math.min(100, Math.round(msg.rms * 400));
      $("level-fill").style.width = pct + "%";
      break;
    }
    case "speech_active":
      setListening(msg.active);
      break;
    case "device":
      setDevice(msg.name, true);
      toast("录音设备：" + msg.name);
      break;
    case "meeting":
      onMeetingEvent(msg.action, msg.meeting);
      break;
    case "error":
      toast(msg.message, true);
      break;
    case "warn":
      toast(msg.message, true);
      break;
    case "info":
      toast(msg.message);
      break;
  }
}

function applyState(s) {
  if (!s) return;
  statusState.model = s.models_loaded ? "ok" : (s.model_error ? "err" : "loading");
  updateStatusPill();
  if (s.device) setDevice(s.device, true);
  if (s.llm_base_url) state.llmBaseUrl = s.llm_base_url;
  if (s.data_dir) { const el = $("about-datadir"); if (el) el.textContent = "数据目录：" + s.data_dir; }
  if (s.voiceprints) { state.voiceprints = s.voiceprints; renderVoiceprints(); }
  if (s.meeting && s.meeting.status !== "ended") {
    state.meeting = s.meeting;
    state.speakers = s.speakers || [];
    state.elapsedBase = (s.recorded_seconds || 0) * 1000;
    enterActiveUI(s.meeting.status);
    loadTranscript(s.meeting.id, true);
  } else {
    state.meeting = null;
    enterIdleUI();
  }
}

/* ---------------- 会议控制 ---------------- */
async function api(path, method = "GET", body = null) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  return r.json();
}

$("btn-start").onclick = async () => {
  const name = $("inp-name").value.trim();
  const dev = $("sel-device").value;
  $("btn-start").disabled = true;
  const r = await api("/api/meeting/start", "POST", {
    name: name || null,
    device_index: dev === "" ? null : parseInt(dev),
  });
  $("btn-start").disabled = false;
  if (!r.ok) toast(r.error || "开始失败", true);
};

$("btn-pause").onclick = async () => {
  const r = await api("/api/meeting/pause", "POST");
  if (!r.ok) toast(r.error || "暂停失败", true);
};
$("btn-resume").onclick = async () => {
  const r = await api("/api/meeting/resume", "POST");
  if (!r.ok) toast(r.error || "继续失败", true);
};
$("btn-end").onclick = async () => {
  if (!confirm("确定结束会议？结束后将生成会议纪要文件。")) return;
  $("btn-end").disabled = true;
  const r = await api("/api/meeting/end", "POST");
  $("btn-end").disabled = false;
  if (!r.ok) toast(r.error || "结束失败", true);
};

function onMeetingEvent(action, meeting) {
  if (action === "started") {
    state.meeting = meeting;
    state.speakers = [];
    state.segments = [];
    state.viewingHistory = false;
    state.elapsedBase = 0;
    hideHistoryBar();
    clearTranscript();
    hideWelcome();
    enterActiveUI("recording");
    addDivider("会议开始 " + nowStr());
    toast("会议已开始：" + meeting.name);
  } else if (action === "paused") {
    state.meeting = meeting;
    state.elapsedBase = Date.now() - state.elapsedStart;  // 冻结已计时长
    enterActiveUI("paused");
    addDivider("已暂停 " + nowStr());
  } else if (action === "resumed") {
    state.meeting = meeting;
    enterActiveUI("recording");
    addDivider("继续 " + nowStr());
  } else if (action === "ended") {
    state.meeting = null;
    enterIdleUI();
    addDivider("会议结束 " + nowStr());
    showSummary(meeting);
  } else if (action === "updated") {
    // 会后自动重分离完成：刷新说话人标注与人名显示
    if (msg.message) toast(msg.message);
    const sm = $("summary-modal");
    if (!sm.classList.contains("hidden") && sm.dataset.mid === msg.meeting.id) showSummary(msg.meeting);
    if (state.viewingHistory && state.historyId === msg.meeting.id) {
      openHistory(msg.meeting.id);
    } else if (!state.viewingHistory && !state.meeting && msg.segments) {
      // 刚结束的实时视图：就地重绘逐字稿（分隔线保留，结束线挪回底部）
      state.speakers = msg.meeting.speakers || [];
      const box = $("transcript");
      const dividers = [...box.querySelectorAll(".divider")];
      const endDiv = dividers[dividers.length - 1];
      box.querySelectorAll(".seg").forEach(el => el.remove());
      state.segments = [];
      const spkMap = {};
      state.speakers.forEach(s => spkMap[s.id] = s);
      for (const seg of msg.segments) {
        const s = spkMap[seg.speaker_id] || {};
        appendSegment({ ...seg,
          speaker_label: s.label || seg.speaker_label,
          speaker_color: s.color || seg.speaker_color });
      }
      if (endDiv) box.appendChild(endDiv);
    }
  }
  refreshMeetingList();
}

function enterActiveUI(status) {
  $("panel-new").classList.add("hidden");
  $("panel-active").classList.remove("hidden");
  $("active-name").textContent = state.meeting.name;
  const paused = status === "paused";
  $("btn-pause").classList.toggle("hidden", paused);
  $("btn-resume").classList.toggle("hidden", !paused);
  startTimer(paused);
}

function enterIdleUI() {
  $("panel-new").classList.remove("hidden");
  $("panel-active").classList.add("hidden");
  stopTimer();
  $("level-fill").style.width = "0%";
  setListening(false);
}

function startTimer(paused) {
  stopTimer();
  if (paused) {
    // 暂停态：显示冻结的已计时长，不再递增
    $("active-timer").textContent = fmtHMS((state.elapsedBase || 0) / 1000);
    return;
  }
  state.elapsedStart = Date.now() - (state.elapsedBase || 0);
  const tick = () => {
    $("active-timer").textContent = fmtHMS((Date.now() - state.elapsedStart) / 1000);
  };
  tick();
  state.timerInt = setInterval(tick, 1000);
}
function stopTimer() { if (state.timerInt) clearInterval(state.timerInt); state.timerInt = null; }

/* ---------------- 转写渲染 ---------------- */
function appendSegment(seg) {
  state.segments.push(seg);
  const spkRole = (state.speakers.find(s => s.id === seg.speaker_id) || {}).role || seg.speaker_role || "";
  const el = document.createElement("div");
  el.className = "seg";
  el.dataset.text = seg.text;
  el.dataset.spk = seg.speaker_id;
  el.innerHTML = `
    <div class="seg-avatar" style="background:${seg.speaker_color}" title="点击查看说话人详情">${seg.speaker_id}</div>
    <div class="seg-body">
      <div class="seg-head">
        <span class="seg-name" style="color:${seg.speaker_color}" title="点击查看说话人详情">${escapeHtml(seg.speaker_label)}</span>
        <span class="seg-role">${spkRole ? "· " + escapeHtml(spkRole) : ""}</span>
        <span class="seg-time">${fmtHMS(seg.start)}</span>
      </div>
      <div class="seg-text">${escapeHtml(seg.text)}</div>
    </div>`;
  // 点击头像或姓名：弹出说话人卡片（改名/颜色/角色/登记声纹）
  const openCard = () => openSpeakerCard(seg.speaker_id);
  el.querySelector(".seg-avatar").onclick = openCard;
  el.querySelector(".seg-name").onclick = openCard;
  $("transcript").appendChild(el);
  applySearchHighlight();
  if (state.autoScroll) $("transcript").scrollTop = $("transcript").scrollHeight;
}

function addDivider(text) {
  hideWelcome();
  const el = document.createElement("div");
  el.className = "divider";
  el.textContent = text;
  $("transcript").appendChild(el);
}

function clearTranscript() {
  $("transcript").innerHTML = "";
  state.segments = [];
}
function hideWelcome() { const w = $("welcome"); if (w) w.remove(); }

/* ---------------- 说话人卡片（点头像/姓名弹出） ---------------- */
const SPEAKER_PALETTE = [
  "#4fc3f7", "#81c784", "#ffb74d", "#f06292", "#ba68c8", "#4db6ac",
  "#fff176", "#ff8a65", "#90a4ae", "#a1887f", "#e57373", "#64b5f6",
];
let scState = { id: null, color: "" };

function buildSwatches(boxId, current, onPick) {
  const box = $(boxId);
  box.innerHTML = "";
  for (const c of SPEAKER_PALETTE) {
    const b = document.createElement("div");
    b.className = "sc-swatch" + (c.toLowerCase() === (current || "").toLowerCase() ? " on" : "");
    b.style.background = c;
    b.onclick = () => {
      box.querySelectorAll(".sc-swatch").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      onPick(c);
    };
    box.appendChild(b);
  }
}

function openSpeakerCard(sid) {
  const spk = state.speakers.find(s => s.id === sid);
  const seg = state.segments.find(s => s.speaker_id === sid);
  if (!spk && !seg) return;
  const label = (spk && spk.label) || (seg && seg.speaker_label) || `说话人 ${sid}`;
  const color = (spk && spk.color) || (seg && seg.speaker_color) || "#4fc3f7";
  const role = (spk && spk.role) || "";
  scState = { id: sid, color };
  $("sc-avatar").textContent = sid;
  $("sc-avatar").style.background = color;
  $("sc-title").textContent = label;
  const cnt = spk ? spk.count : state.segments.filter(s => s.speaker_id === sid).length;
  const secs = spk ? spk.seconds : 0;
  $("sc-stats").textContent = `${cnt} 次发言 · ${Math.round(secs)}s` + (spk && spk.vp_id ? " · 已登记声纹" : "");
  $("sc-name").value = label;
  $("sc-role").value = role;
  buildSwatches("sc-colors", color, c => {
    scState.color = c;
    $("sc-avatar").style.background = c;
  });
  // 登记声纹只在查看历史会议时可用（需要完整录音提取声纹）
  const canEnroll = !!(state.viewingHistory && state.historyId) && !(spk && spk.vp_id);
  $("btn-sc-enroll").classList.toggle("hidden", !canEnroll);
  $("speaker-card").classList.remove("hidden");
  setTimeout(() => $("sc-name").focus(), 60);
}
$("btn-sc-close").onclick = () => $("speaker-card").classList.add("hidden");

$("btn-sc-save").onclick = async () => {
  const sid = scState.id;
  if (sid === null) return;
  const name = $("sc-name").value.trim();
  if (!name) return toast("姓名不能为空", true);
  const body = { speaker_id: sid, name, color: scState.color, role: $("sc-role").value };
  let r;
  if (state.viewingHistory && state.historyId) {
    r = await api(`/api/meetings/${state.historyId}/update_speaker`, "POST", body);
    if (!r.ok) return toast(r.error || "保存失败", true);
    toast(`已保存：「${name}」`);
    $("speaker-card").classList.add("hidden");
    openHistory(state.historyId);  // 重新加载，刷新标签与转写
  } else if (state.meeting) {
    r = await api("/api/meeting/update_speaker", "POST", body);
    if (!r.ok) return toast(r.error || "保存失败", true);
    toast(`已保存：「${name}」（实时生效）`);
    $("speaker-card").classList.add("hidden");
    // 界面更新由 WebSocket 的 speaker_renamed 广播完成
  } else {
    toast("当前没有进行中的会议", true);
  }
};

$("btn-sc-enroll").onclick = () => {
  $("speaker-card").classList.add("hidden");
  if (state.viewingHistory && state.historyId && scState.id !== null) {
    enrollSpeaker(state.historyId, scState.id);
  }
};

/* ---------------- 声纹人员卡片（点声纹库人员弹出） ---------------- */
let vcState = { id: null, color: "" };

function openVpCard(vid) {
  const v = (state.voiceprints || []).find(x => x.id === vid);
  if (!v) return;
  vcState = { id: vid, color: v.color };
  $("vc-avatar").textContent = (v.name || "声").slice(0, 1);
  $("vc-avatar").style.background = v.color;
  $("vc-title").textContent = v.name;
  const info = [];
  if (v.source) info.push("来自：" + v.source);
  if (v.created_at) info.push("登记于 " + String(v.created_at).replace("T", " "));
  $("vc-stats").textContent = info.join(" · ") || "已登记声纹";
  $("vc-name").value = v.name;
  $("vc-role").value = v.role || "";
  buildSwatches("vc-colors", v.color, c => {
    vcState.color = c;
    $("vc-avatar").style.background = c;
  });
  $("vp-card").classList.remove("hidden");
  setTimeout(() => $("vc-name").focus(), 60);
}
$("btn-vc-close").onclick = () => $("vp-card").classList.add("hidden");

$("btn-vc-save").onclick = async () => {
  if (!vcState.id) return;
  const name = $("vc-name").value.trim();
  if (!name) return toast("姓名不能为空", true);
  const r = await api(`/api/voiceprints/${vcState.id}/update`, "POST",
    { name, color: vcState.color, role: $("vc-role").value });
  if (!r.ok) return toast(r.error || "保存失败", true);
  toast(`已保存：「${name}」`);
  $("vp-card").classList.add("hidden");
  refreshVoiceprints();
};

$("btn-vc-del").onclick = async () => {
  const v = (state.voiceprints || []).find(x => x.id === vcState.id);
  if (!confirm(`确定从声纹库删除「${v ? v.name : vcState.id}」？`)) return;
  const r = await fetch(`/api/voiceprints/${vcState.id}`, { method: "DELETE" }).then(x => x.json());
  if (!r.ok) return toast(r.error || "删除失败", true);
  toast("已删除");
  $("vp-card").classList.add("hidden");
  refreshVoiceprints();
};

/* ---------------- 通用输入弹窗（替代 window.prompt，WebView2 不支持 prompt） ---------------- */
function uiPrompt(title, def = "") {
  return new Promise(resolve => {
    $("prompt-title").textContent = title;
    const inp = $("prompt-input");
    inp.value = def;
    $("prompt-modal").classList.remove("hidden");
    setTimeout(() => { inp.focus(); inp.select(); }, 60);
    const done = v => {
      $("prompt-modal").classList.add("hidden");
      $("btn-prompt-ok").onclick = $("btn-prompt-cancel").onclick = inp.onkeydown = null;
      resolve(v);
    };
    $("btn-prompt-ok").onclick = () => done(inp.value);
    $("btn-prompt-cancel").onclick = () => done(null);
    inp.onkeydown = e => {
      e.stopPropagation();
      if (e.key === "Enter") done(inp.value);
      if (e.key === "Escape") done(null);
    };
  });
}

async function renameLiveSpeaker(speakerId) {
  const cur = (state.speakers.find(s => s.id === speakerId) || {}).label || `说话人 ${speakerId}`;
  const name = await uiPrompt(`将「${cur}」改名为（实时生效，已上屏内容同步更新）：`, cur);
  if (name === null) return;
  if (!name.trim()) return toast("姓名不能为空", true);
  const r = await api("/api/meeting/rename_speaker", "POST",
    { speaker_id: speakerId, name: name.trim() });
  if (!r.ok) return toast(r.error || "改名失败", true);
  toast(`已改名为「${name.trim()}」`);
}

/* ---------------- 固定声纹库 ---------------- */
function renderVoiceprints() {
  const list = $("vp-list");
  if (!list) return;
  const vps = state.voiceprints || [];
  if (!vps.length) {
    list.innerHTML = '<div class="empty">暂无。点击对话中的头像可将说话人登记进声纹库</div>';
    return;
  }
  list.innerHTML = vps.map(v => `
    <div class="vp-item clickable" data-id="${v.id}" title="点击查看 / 编辑详情">
      <span class="spk-dot" style="background:${v.color}"></span>
      <span class="vp-name">${escapeHtml(v.name)}</span>
      <span class="vp-src">${v.role ? escapeHtml(v.role) : (v.source ? "来自：" + escapeHtml(v.source) : "")}</span>
    </div>`).join("");
  list.querySelectorAll(".vp-item").forEach(el => {
    el.onclick = () => openVpCard(el.dataset.id);
  });
}

async function refreshVoiceprints() {
  const r = await api("/api/voiceprints");
  state.voiceprints = r.voiceprints || [];
  renderVoiceprints();
}

async function enrollSpeaker(mid, speakerId) {
  const cur = (state.speakers.find(s => s.id === speakerId) || {}).label || `说话人 ${speakerId}`;
  const name = await uiPrompt(`将「${cur}」存入声纹库，输入真实姓名：\n（以后的会议会自动识别出这个人）`, cur.startsWith("说话人") ? "" : cur);
  if (name === null) return;
  if (!name.trim()) return toast("姓名不能为空", true);
  toast("正在提取声纹…");
  const r = await api(`/api/meetings/${mid}/enroll_speaker`, "POST",
    { speaker_id: speakerId, name: name.trim() });
  if (!r.ok) return toast(r.error || "存入失败", true);
  toast(`已存入声纹库：「${name.trim()}」（采用 ${r.voiceprint.samples_used} 段发言）`);
  refreshVoiceprints();
  openHistory(mid);  // 此人标签已同步为注册名，刷新显示
}

async function renameSpeaker(mid, speakerId) {
  const cur = (state.speakers.find(s => s.id === speakerId) || {}).label || `说话人 ${speakerId}`;
  const name = await uiPrompt(`将「${cur}」重命名为（例如真实姓名）：`, cur);
  if (name === null) return;
  if (!name.trim()) return toast("姓名不能为空", true);
  const r = await api(`/api/meetings/${mid}/rename_speaker`, "POST",
    { speaker_id: speakerId, name: name.trim() });
  if (!r.ok) return toast(r.error || "重命名失败", true);
  toast(`已重命名为「${name.trim()}」`);
  openHistory(mid);  // 重新加载，刷新标签与转写
}

/* ---------------- 历史会议 ---------------- */
async function refreshMeetingList() {
  const r = await api("/api/meetings");
  const list = $("meeting-list");
  const ms = (r.meetings || []);
  if (!ms.length) { list.innerHTML = '<div class="empty">暂无记录</div>'; return; }
  list.innerHTML = ms.map(m => `
    <div class="meeting-item" data-id="${m.id}" data-name="${escapeHtml(m.name)}">
      <div class="mi-name">${escapeHtml(m.name)}
        <span class="mi-badge ${m.status}">${{recording:"录音中",paused:"已暂停",ended:"已结束"}[m.status]||m.status}</span>
        ${m.has_summary ? '<span class="mi-badge summary">AI纪要</span>' : ""}
      </div>
      <div class="mi-meta">${(m.created_at||"").replace("T"," ")} · ${fmtDur(m.recorded_seconds)}</div>
      <div class="mi-actions">
        <span class="mi-act mi-edit" title="重命名会议">✎</span>
        <span class="mi-act mi-del" title="删除会议">🗑</span>
      </div>
    </div>`).join("");
  list.querySelectorAll(".meeting-item").forEach(el => {
    el.onclick = () => openHistory(el.dataset.id);
    el.querySelector(".mi-edit").onclick = (ev) => {
      ev.stopPropagation();
      renameMeeting(el.dataset.id, el.dataset.name);
    };
    el.querySelector(".mi-del").onclick = (ev) => {
      ev.stopPropagation();
      deleteMeeting(el.dataset.id, el.dataset.name);
    };
  });
}

async function renameMeeting(mid, curName) {
  const name = await uiPrompt("重命名会议：", curName);
  if (name === null) return;
  if (!name.trim()) return toast("名称不能为空", true);
  const r = await api(`/api/meetings/${mid}/rename`, "POST", { name: name.trim() });
  if (!r.ok) return toast(r.error || "重命名失败", true);
  toast(`已重命名为「${name.trim()}」`);
  refreshMeetingList();
  if (state.historyId === mid) openHistory(mid);  // 正在查看则同步刷新标题
}

async function deleteMeeting(mid, name) {
  if (!confirm(`确定删除会议「${name}」？\n\n录音、转写和 AI 纪要将一并删除，不可恢复。`)) return;
  const r = await fetch(`/api/meetings/${mid}`, { method: "DELETE" }).then(x => x.json());
  if (!r.ok) return toast(r.error || "删除失败", true);
  toast("会议已删除");
  if (state.historyId === mid) {
    exitHistoryView();
    state.speakers = [];
    $("transcript").innerHTML = `
      <div class="welcome" id="welcome">
        <div class="welcome-icon">🎙️</div>
        <h1>会议实时转写公屏</h1>
        <p>全程本地运行，无需联网。点击左侧「开始会议」，<br>
           每位参会者的每句话将实时转写并自动区分说话人。</p>
      </div>`;
  }
  refreshMeetingList();
}

async function openHistory(mid) {
  const r = await api(`/api/meetings/${mid}`);
  if (!r.meeting) return toast("读取失败", true);
  state.viewingHistory = true;
  state.historyId = mid;
  state.historySeconds = r.meeting.recorded_seconds || 0;
  clearTranscript();
  addDivider(`查看历史会议：${r.meeting.name}`);
  state.speakers = r.meeting.speakers || [];
  for (const seg of r.transcript) {
    appendSegment({
      ...seg,
      speaker_label: (state.speakers.find(s => s.id === seg.speaker_id) || {}).label || seg.speaker_label,
      speaker_color: (state.speakers.find(s => s.id === seg.speaker_id) || {}).color || seg.speaker_color,
    });
  }
  addDivider("—— 历史记录结束，新会议开始时自动回到实时视图 ——");
  showHistoryBar(r.meeting);
}

/* ---------------- 自定义音频播放器 ---------------- */
function AudioPlayer(playerId, audioId) {
  const audio = $(audioId);
  const box = $(playerId);
  const btn = box.querySelector(".aplay-btn");
  const seek = box.querySelector(".aplay-seek");
  const times = box.querySelectorAll(".aplay-time");
  const cur = times[0], dur = times[1];
  const fmt = s => isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "0:00";
  btn.onclick = () => { if (audio.src) audio.paused ? audio.play() : audio.pause(); };
  audio.addEventListener("play", () => btn.textContent = "⏸");
  audio.addEventListener("pause", () => btn.textContent = "▶");
  audio.addEventListener("loadedmetadata", () => dur.textContent = fmt(audio.duration));
  audio.addEventListener("timeupdate", () => {
    cur.textContent = fmt(audio.currentTime);
    if (!seek.dataset.drag && audio.duration) seek.value = Math.round(audio.currentTime / audio.duration * 1000);
  });
  audio.addEventListener("emptied", () => {
    btn.textContent = "▶"; cur.textContent = "0:00"; dur.textContent = "0:00"; seek.value = 0;
  });
  seek.addEventListener("pointerdown", () => seek.dataset.drag = "1");
  seek.addEventListener("change", () => {
    delete seek.dataset.drag;
    if (audio.duration) audio.currentTime = seek.value / 1000 * audio.duration;
  });
  seek.addEventListener("input", () => { if (audio.duration) cur.textContent = fmt(seek.value / 1000 * audio.duration); });
}

function showHistoryBar(m) {
  const bar = $("history-bar");
  bar.classList.remove("hidden");
  const audio = $("history-audio");
  if (m.has_audio !== false) {
    audio.src = `/api/meetings/${m.id}/audio`;
    $("history-player").classList.remove("hidden");
  } else {
    audio.removeAttribute("src");
    audio.load();
    $("history-player").classList.add("hidden");
  }
  $("hb-audio-dl").href = `/api/meetings/${m.id}/audio`;
  $("hb-txt-dl").href = `/api/meetings/${m.id}/transcript.txt`;
  const mdDl = $("hb-md-dl");
  mdDl.href = `/api/meetings/${m.id}/summary.md`;
  mdDl.classList.toggle("hidden", !m.has_summary);
  $("btn-hb-minutes").textContent = m.has_summary ? "📝 查看 / 重新生成 AI 纪要" : "📝 生成 AI 纪要";
}

function hideHistoryBar() {
  $("history-bar").classList.add("hidden");
  const audio = $("history-audio");
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
}

function exitHistoryView() {
  state.viewingHistory = false;
  state.historyId = null;
  hideHistoryBar();
  clearTranscript();
}

/* ---------------- 总结弹窗 ---------------- */
function showSummary(m) {
  $("summary-modal").dataset.mid = m.id;
  const spkSeconds = (m.speakers || []).map(s => s.seconds || 0);
  $("summary-grid").innerHTML = `
    <div class="summary-cell"><div class="num">${fmtHMS(m.recorded_seconds || 0)}</div><div class="lbl">录音时长</div></div>
    <div class="summary-cell"><div class="num">${(m.speakers || []).length}</div><div class="lbl">说话人数</div></div>
    <div class="summary-cell"><div class="num">${m.segment_count || 0}</div><div class="lbl">发言条数</div></div>
    <div class="summary-cell"><div class="num">${fmtHMS(Math.max(0, ...spkSeconds))}</div><div class="lbl">最长单人发言</div></div>`;
  $("summary-files").innerHTML = `
    <a href="/api/meetings/${m.id}/audio" download>⬇ 会议录音 (.wav)</a>
    <a href="/api/meetings/${m.id}/transcript.txt" download>⬇ 会议纪要 (.txt)</a>`;
  $("summary-audio").src = `/api/meetings/${m.id}/audio`;
  $("btn-gen-minutes").onclick = () => {
    $("summary-modal").classList.add("hidden");
    generateMinutes(m.id);
  };
  $("summary-modal").classList.remove("hidden");
}
$("btn-close-summary").onclick = () => $("summary-modal").classList.add("hidden");

/* ---------------- AI 会议纪要 ---------------- */

/* 轻量 Markdown 渲染器（全离线，无外部依赖）：标题/加粗/列表/表格/分割线/代码 */
function mdToHtml(md) {
  const lines = escapeHtml(md).split("\n");
  const out = [];
  let list = null, table = null;
  const inline = (s) => s
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const isSepRow = (cells) => cells.every(c => /^:?-{2,}:?$/.test(c.replace(/\s/g, "")));
  const flushTable = () => {
    if (!table) return;
    let head = null, rows = table;
    if (table.length >= 2 && isSepRow(table[1])) { head = table[0]; rows = table.slice(2); }
    let h = "<table>";
    if (head) h += "<thead><tr>" + head.map(c => `<th>${c}</th>`).join("") + "</tr></thead>";
    h += "<tbody>" + rows.map(r => "<tr>" + r.map(c => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>";
    out.push(h);
    table = null;
  };
  for (const raw of lines) {
    const t = raw.trim();
    if (/^\|.*\|$/.test(t)) {   // 表格行
      flushList();
      const cells = t.slice(1, -1).split("|").map(c => inline(c.trim()));
      (table = table || []).push(cells);
      continue;
    }
    flushTable();
    let m;
    if (!t) { flushList(); continue; }
    if ((m = t.match(/^(#{1,4})\s+(.*)/))) {
      flushList();
      const lv = m[1].length;
      out.push(`<h${lv}>${inline(m[2])}</h${lv}>`);
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(t)) { flushList(); out.push("<hr>"); continue; }
    if ((m = t.match(/^[-*•]\s+(.*)/))) {
      if (list !== "ul") { flushList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    if ((m = t.match(/^\d+[.、]\s*(.*)/))) {
      if (list !== "ol") { flushList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    flushList();
    out.push(`<p>${inline(t)}</p>`);
  }
  flushList(); flushTable();
  return out.join("\n");
}

function setMinutesStatus(text, show) {
  const el = $("minutes-status");
  el.textContent = text || "";
  el.classList.toggle("hidden", !show);
}

function openMinutesModal(mid) {
  $("minutes-modal").classList.remove("hidden");
  $("minutes-download").href = `/api/meetings/${mid}/summary.md`;
  $("btn-minutes-regen").onclick = () => {
    if (confirm("重新生成将覆盖已有纪要，确定吗？")) generateMinutes(mid, true);
  };
}

/* 生成（或查看）AI 纪要；skipExisting=true 时强制重新生成 */
async function generateMinutes(mid, skipExisting) {
  // 已有纪要且非强制：先展示，再由用户决定是否重新生成
  if (!skipExisting) {
    const s = await api(`/api/meetings/${mid}/summary`);
    if (s.has_summary) {
      openMinutesModal(mid);
      $("minutes-body").innerHTML = mdToHtml(s.content);
      setMinutesStatus("", false);
      $("minutes-download").classList.remove("hidden");
      $("btn-minutes-regen").classList.remove("hidden");
      return;
    }
  }
  // 先探测本地大模型
  const st = await api("/api/llm/status");
  if (!st.available) { showLlmModal(mid); return; }

  openMinutesModal(mid);
  $("minutes-body").innerHTML = "";
  $("minutes-download").classList.add("hidden");
  $("btn-minutes-regen").classList.add("hidden");
  setMinutesStatus("⏳ 正在调用本地大模型生成纪要（27B 模型较慢，长会议可能需要几分钟）…", true);

  let resp;
  try {
    resp = await fetch(`/api/meetings/${mid}/summary`, { method: "POST" });
  } catch (e) {
    setMinutesStatus("", false);
    showLlmModal(mid);
    return;
  }
  if (resp.status === 503) { setMinutesStatus("", false); showLlmModal(mid); return; }
  if (!resp.ok || !resp.body) {
    const j = await resp.json().catch(() => ({}));
    setMinutesStatus("", false);
    $("minutes-body").innerHTML = `<p class="minutes-err">生成失败：${escapeHtml(j.error || resp.statusText)}</p>`;
    return;
  }

  // 读取 SSE 流，边生成边渲染
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "", text = "", done = false;
  while (!done) {
    const { done: rd, value } = await reader.read();
    if (rd) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 2);
      if (!line.startsWith("data:")) continue;
      let obj;
      try { obj = JSON.parse(line.slice(5).trim()); } catch { continue; }
      if (obj.token) {
        setMinutesStatus("⏳ 正在生成…", true);
        text += obj.token;
        $("minutes-body").innerHTML = mdToHtml(text);
        $("minutes-body").scrollTop = $("minutes-body").scrollHeight;
      }
      if (obj.error) {
        setMinutesStatus("", false);
        $("minutes-body").innerHTML = `<p class="minutes-err">生成失败：${escapeHtml(obj.error)}</p>`;
        done = true;
        break;
      }
      if (obj.done) {
        done = true;
        setMinutesStatus("", false);
        if (text.trim()) {
          $("minutes-download").classList.remove("hidden");
          $("btn-minutes-regen").classList.remove("hidden");
          toast("✅ AI 纪要已生成并保存");
          refreshMeetingList();
        } else {
          $("minutes-body").innerHTML = '<p class="minutes-err">大模型未返回内容</p>';
        }
        break;
      }
    }
  }
}

/* 本地大模型未启动提示弹窗 */
function showLlmModal(pendingMid) {
  $("llm-addr").textContent = state.llmBaseUrl || "http://127.0.0.1:8080";
  $("llm-modal").classList.remove("hidden");
  $("btn-llm-retry").onclick = async () => {
    $("btn-llm-retry").disabled = true;
    $("btn-llm-retry").textContent = "检测中…";
    const st = await api("/api/llm/status");
    $("btn-llm-retry").disabled = false;
    $("btn-llm-retry").textContent = "🔄 重新检测";
    if (st.available) {
      $("llm-modal").classList.add("hidden");
      toast("✅ 已检测到本地大模型");
      if (pendingMid) generateMinutes(pendingMid, true);
    } else {
      toast("仍未检测到大模型服务，请确认 llama-server 已启动", true);
    }
  };
}
$("btn-llm-close").onclick = () => $("llm-modal").classList.add("hidden");
$("btn-close-minutes").onclick = () => $("minutes-modal").classList.add("hidden");
$("btn-hb-minutes").onclick = () => { if (state.historyId) generateMinutes(state.historyId); };

/* ---------------- 会后离线重分离 ---------------- */
$("btn-hb-rediarize").onclick = async () => {
  const mid = state.historyId;
  if (!mid) return;
  const cur = state.speakers.length || "";
  const input = await uiPrompt(
    "用离线模型对整段录音重新计算说话人（比实时标注更准）。\n\n" +
    "请输入本场会议的实际参会人数（留空则自动判断，远场环境自动模式可能不准，建议填人数）：", String(cur));
  if (input === null) return;
  const n = input.trim() === "" ? null : parseInt(input.trim());
  if (input.trim() !== "" && (!Number.isInteger(n) || n < 1 || n > 20)) {
    return toast("请输入 1-20 之间的人数", true);
  }
  const est = Math.max(1, Math.round((state.historySeconds || 600) / 6));
  if (!confirm(`将重新分离说话人并覆盖现有标注（已改过的名字会尽量按时间对应保留）。\n` +
               `预计耗时约 ${est} 秒，期间请耐心等待。确定继续吗？`)) return;

  const btn = $("btn-hb-rediarize");
  btn.disabled = true;
  btn.textContent = "⏳ 正在重新分离…";
  try {
    const r = await fetch(`/api/meetings/${mid}/rediarize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ num_clusters: n }),
    }).then(x => x.json());
    if (!r.ok) throw new Error(r.error || "分离失败");
    toast(`✅ 重分离完成：${r.speakers.length} 位说话人，${r.changed}/${r.total} 句标注已更新`);
    await openHistory(mid);
    if (r.meeting && r.meeting.summary_stale) {
      toast("说话人已变化，建议重新生成 AI 纪要", true);
    }
    refreshMeetingList();
  } catch (e) {
    toast(e.message || "分离失败", true);
  } finally {
    btn.disabled = false;
    btn.textContent = "🎚 重新分离说话人";
  }
};

/* ---------------- 工具 ---------------- */
function setDevice(name, ok) {
  statusState.device = ok ? (name || "") : "";
  updateStatusPill();
}
function setListening(on) {
  const el = $("listening");
  el.classList.toggle("on", on);
  el.textContent = on ? "正在聆听…" : "待命中";
}
function toast(text, isErr) {
  const t = $("toast");
  t.textContent = text;
  t.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 3200);
}
function fmtHMS(t) {
  t = Math.max(0, Math.floor(t));
  const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}
function fmtDur(t) {
  t = Math.max(0, Math.round(t || 0));
  if (t >= 3600) return fmtHMS(t);
  if (t >= 60) return `${Math.floor(t / 60)}分${t % 60 ? t % 60 + "秒" : ""}`;
  return `${t}秒`;
}
function nowStr() { return new Date().toLocaleTimeString("zh-CN", { hour12: false }); }
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

async function loadTranscript(mid, silent) {
  const r = await api(`/api/meetings/${mid}`);
  if (!r.transcript) return;
  clearTranscript();
  hideWelcome();
  for (const seg of r.transcript) appendSegment(seg);
}

/* 搜索高亮 */
$("inp-search").oninput = applySearchHighlight;
function applySearchHighlight() {
  const q = $("inp-search").value.trim();
  document.querySelectorAll("#transcript .seg").forEach(el => {
    const txtEl = el.querySelector(".seg-text");
    const raw = el.dataset.text || "";
    if (!q) { txtEl.innerHTML = escapeHtml(raw); el.style.display = ""; return; }
    const hit = raw.toLowerCase().includes(q.toLowerCase());
    el.style.display = hit ? "" : "none";
    if (hit) {
      const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
      txtEl.innerHTML = escapeHtml(raw).replace(re, m => `<mark>${m}</mark>`);
    }
  });
}

/* ---------------- 全屏（客户端走原生窗口，浏览器走 Fullscreen API） ---------------- */
function toggleFullscreen() {
  const napi = window.pywebview && window.pywebview.api;
  if (napi && napi.toggle_fullscreen) {
    // 客户端模式：Fullscreen API 在 WebView2 宿主里无效，用原生窗口全屏
    napi.toggle_fullscreen();
    state.nativeFs = !state.nativeFs;
    toast(state.nativeFs ? "已进入全屏" : "已退出全屏");
    return;
  }
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else {
    document.documentElement.requestFullscreen().catch(() =>
      toast("当前环境不支持全屏", true));
  }
}

/* ---------------- 设置抽屉（右侧滑出） ---------------- */
function fillModelOptions(models, current) {
  const sel = $("set-model");
  sel.innerHTML = "";
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "自动（服务器默认）";
  sel.appendChild(auto);
  (models || []).forEach(m => {
    const o = document.createElement("option");
    o.value = m; o.textContent = m;
    sel.appendChild(o);
  });
  if (current && !(models || []).includes(current)) {
    const o = document.createElement("option");
    o.value = current; o.textContent = current + "（已保存）";
    sel.appendChild(o);
  }
  sel.value = current || "";
}

let _modelDetectTimer = null;
function detectModels(showMsg) {
  const base = $("set-base-url").value.trim();
  const current = $("set-model").value;
  if (!base) { fillModelOptions([], current); return; }
  if (showMsg) $("set-status").textContent = "正在检测模型列表…";
  api("/api/settings/llm/models", "POST", {
    base_url: base,
    api_key: $("set-api-key").value,
  }).then(r => {
    if (r.ok) {
      fillModelOptions(r.models, current);
      if (showMsg) $("set-status").textContent = `✅ 检测到 ${r.models.length} 个可用模型`;
    } else {
      fillModelOptions([], current);
      if (showMsg) $("set-status").textContent = "⚠️ " + (r.error || "模型检测失败");
    }
  });
}
function scheduleDetectModels() {
  clearTimeout(_modelDetectTimer);
  _modelDetectTimer = setTimeout(() => detectModels(false), 800);
}

function openSettings() {
  $("settings-drawer").classList.remove("hidden");
  $("drawer-mask").classList.remove("hidden");
  applyPrefs();  // 同步显示类控件状态
  api("/api/settings/llm").then(r => {
    $("set-base-url").value = r.base_url || "";
    $("set-api-key").value = r.api_key || "";
    fillModelOptions([], r.model || "");
    $("set-status").textContent = "";
    if (r.base_url) detectModels(false);  // 打开设置时自动检测一次
  });
  api("/api/settings/client").then(r => {
    $("set-close-action").value = r.close_action || "ask";
  });
}
function closeSettings() {
  $("settings-drawer").classList.add("hidden");
  $("drawer-mask").classList.add("hidden");
}
$("btn-settings").onclick = openSettings;
$("btn-set-close").onclick = closeSettings;
$("drawer-mask").onclick = closeSettings;

/* 显示：字号分段 */
document.querySelectorAll("#set-font button").forEach(b => {
  b.onclick = () => {
    state.fontLevel = parseInt(b.dataset.v);
    applyPrefs();
    savePrefs();
  };
});
/* 显示：自动滚动 / 时间戳开关 */
$("set-autoscroll").onclick = () => {
  state.autoScroll = !state.autoScroll;
  applyPrefs();
  savePrefs();
  if (state.autoScroll) $("transcript").scrollTop = $("transcript").scrollHeight;
};
$("set-timestamp").onclick = () => {
  state.showTime = !state.showTime;
  applyPrefs();
  savePrefs();
};
/* 显示：全屏 */
$("set-fullscreen").onclick = toggleFullscreen;

/* 设置：AI 纪要大模型 */
$("set-base-url").addEventListener("input", scheduleDetectModels);
$("set-api-key").addEventListener("input", scheduleDetectModels);
$("btn-model-refresh").onclick = () => detectModels(true);

/* 设置：关闭窗口行为 */
$("set-close-action").onchange = () => {
  api("/api/settings/client", "POST", { close_action: $("set-close-action").value });
};

/* 关闭窗口选择弹窗（desktop_app.py closing 拦截后调用 msAskClose） */
window.msAskClose = function () {
  $("close-mask").classList.remove("hidden");
};
$("btn-close-cancel").onclick = () => {
  $("close-mask").classList.add("hidden");
};
$("btn-close-ok").onclick = () => {
  const act = (document.querySelector("input[name=closeAct]:checked") || {}).value || "tray";
  const rem = $("close-remember").checked;
  $("close-mask").classList.add("hidden");
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.perform_close(act, rem);
  }
};

$("btn-set-save").onclick = async () => {
  const btn = $("btn-set-save");
  btn.disabled = true;
  btn.textContent = "保存并检测中…";
  const r = await api("/api/settings/llm", "POST", {
    base_url: $("set-base-url").value,
    api_key: $("set-api-key").value,
    model: $("set-model").value,
  });
  btn.disabled = false;
  btn.textContent = "保存并检测";
  if (!r.ok) { $("set-status").textContent = "❌ " + (r.error || "保存失败"); return; }
  state.llmBaseUrl = r.base_url;
  $("set-status").textContent = r.available
    ? `✅ 已保存，连接正常（模型：${r.model || "自动获取"}）`
    : "⚠️ 已保存，但暂时连不上该服务。生成纪要前请确认服务已启动、地址和 Key 正确。";
};

/* ---------------- 设备列表（可手动刷新） ---------------- */
function updateDeviceAvail() {
  // 设备就绪判定：能枚举到录音设备即为「就绪」（设备在会议开始时才真正打开）
  const devs = state.devList || [];
  let name = "";
  if (devs.length) {
    const sel = $("sel-device");
    const kw = (state.devKeyword || "").toLowerCase();
    const chosen = sel.value !== "" ? devs.find(x => String(x.index) === sel.value)
      : (devs.find(x => kw && x.name.toLowerCase().includes(kw)) || devs[0]);
    name = (chosen || devs[0]).name;
  }
  statusState.deviceAvail = name;
  updateStatusPill();
}

async function loadDevices(notify) {
  const d = await api("/api/devices");
  const sel = $("sel-device");
  const cur = sel.value;
  sel.innerHTML = '<option value="">自动选择录音设备</option>';
  for (const dev of d.devices || []) {
    const opt = document.createElement("option");
    opt.value = dev.index;
    opt.textContent = `${dev.name} (${dev.rate}Hz)`;
    sel.appendChild(opt);
  }
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
  state.devList = d.devices || [];
  state.devKeyword = d.keyword || "";
  updateDeviceAvail();
  if (notify) toast(`检测到 ${(d.devices || []).length} 个录音设备`);
}
$("btn-dev-refresh").onclick = () => loadDevices(true);
$("sel-device").onchange = updateDeviceAvail;

/* ---------------- 下载：客户端内走 Windows 保存对话框 ---------------- */
async function nativeDownload(href) {
  const api = window.pywebview && window.pywebview.api;
  if (!api || !api.save_file) return false;  // 浏览器模式：保持默认下载行为
  const path = new URL(href, location.origin).pathname;
  const saved = await api.save_file(path, "");
  if (saved && !saved.startsWith("ERROR:")) toast("已保存：" + saved);
  else if (saved && saved.startsWith("ERROR:")) toast(saved.slice(6), true);
  return true;  // 用户取消也视为已处理
}
document.addEventListener("click", e => {
  const a = e.target.closest("a[download]");
  if (!a) return;
  if (window.pywebview && window.pywebview.api) {
    e.preventDefault();
    nativeDownload(a.href);
  }
});

/* ---------------- 初始化 ---------------- */
(async function init() {
  loadPrefs();
  connect();
  refreshMeetingList();
  loadDevices(false);
  AudioPlayer("history-player", "history-audio");
  AudioPlayer("summary-player", "summary-audio");
  applyPrefs();
})();

// 客户端模式心跳：告知后端本页面仍开着，窗口关闭后客户端自动停止服务
setInterval(() => { fetch("/api/heartbeat", { method: "POST" }).catch(() => {}); }, 5000);
fetch("/api/heartbeat", { method: "POST" }).catch(() => {});
