/* sessions.js — merged Live + Historical. Subagents nested under parent. */

let _sessionFilter = "";
const _openDays = new Set();
// Reuses _openSubs from live.js (single shared expand-state across nested + live subagent rows).
let _initialized = false;

const ITALIAN_DAYS  = ["Dom", "Lun", "Mar", "Mer", "Gio", "Ven", "Sab"];
const ITALIAN_MONTHS = ["Gen","Feb","Mar","Apr","Mag","Giu","Lug","Ago","Set","Ott","Nov","Dic"];

function fmtDateFriendly(iso) {
  if (!iso) return "";
  const today = new Date();
  const todayIso = today.toISOString().slice(0, 10);
  const yest = new Date(today.getTime() - 86400000).toISOString().slice(0, 10);
  if (iso === todayIso) return "Oggi";
  if (iso === yest) return "Ieri";
  const d = new Date(iso + "T00:00:00Z");
  return `${ITALIAN_DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${ITALIAN_MONTHS[d.getUTCMonth()]}`;
}

function _matchesFilter(s, q) {
  if (!q) return true;
  return (s.topic || "").toLowerCase().includes(q) ||
         (s.agent || "").toLowerCase().includes(q) ||
         (s.parentAgent || "").toLowerCase().includes(q) ||
         (s.modelDisplay || "").toLowerCase().includes(q);
}

function renderSessions() {
  const d = getData();
  const all = (d.sessions || []).slice();
  const filt = _sessionFilter.toLowerCase().trim();

  // ─── Live group: live cards from currentByAgent ───
  renderLiveGroup(d, filt);

  // ─── Historical: only main + cron + orphan-subagents at top level.
  //     Subagents matched to a parent are nested INSIDE the parent's card.
  const topLevel = all.filter(s => {
    if (s.kind === "subagent" && s.parentSessionKey) return false;
    if (s.isLive) return false;  // live ones are already shown above
    return _matchesFilter(s, filt);
  });

  // For relative cost bar widths
  const maxCost = Math.max(...all.map(s => s.cost || 0), 0.0001);

  const list = document.getElementById("sessionsList");
  const count = document.getElementById("sessionsCount");
  if (count) {
    count.textContent = filt
      ? `${topLevel.length}/${all.length} sessioni`
      : `${all.length} sessioni · ${fmtUsd(d.stats?.totalCostFromSessionsUsd || 0)}`;
  }

  if (!topLevel.length) {
    list.innerHTML = filt
      ? `<div class="empty muted">Nessun risultato per "${escHtml(filt)}"</div>`
      : '<div class="empty muted">Nessuna sessione storica</div>';
    return;
  }

  // Group by date
  const groups = new Map();
  for (const s of topLevel) {
    if (!groups.has(s.date)) groups.set(s.date, []);
    groups.get(s.date).push(s);
  }

  if (!_initialized) {
    const first = groups.keys().next().value;
    if (first) _openDays.add(first);
    _initialized = true;
  }

  const parts = [];
  for (const [date, items] of groups) {
    // Day totals include nested subagents that match the filter
    let dayCost = 0, dayTokIn = 0, dayTokOut = 0, dayCount = 0;
    for (const s of items) {
      dayCost += s.cost || 0;
      dayTokIn += s.tokensIn || 0;
      dayTokOut += s.tokensOut || 0;
      dayCount += 1;
      for (const sb of (s.subagents || [])) {
        if (!_matchesFilter({topic: sb.topic, agent: sb.agent, modelDisplay: sb.modelDisplay}, filt)) continue;
        dayCost += sb.cost || 0;
        dayTokIn += sb.tokensIn || 0;
        dayTokOut += sb.tokensOut || 0;
        dayCount += 1;
      }
    }
    const open = _openDays.has(date);
    parts.push(`
      <div class="day-group ${open ? "open" : ""}" data-date="${date}">
        <button class="day-head" data-toggle-day="${date}" aria-expanded="${open}">
          <span class="day-chevron">${open ? "▾" : "▸"}</span>
          <div class="day-titles">
            <span class="day-name">${fmtDateFriendly(date)}</span>
            <span class="day-iso muted">${escHtml(date)}</span>
          </div>
          <div class="day-numbers">
            <span class="day-cost">${fmtUsd(dayCost)}</span>
            <span class="day-meta muted">${dayCount} sess · ${fmtNum(dayTokIn + dayTokOut)} tok</span>
          </div>
        </button>
        <div class="day-body" ${open ? "" : 'hidden'}>
          ${items.map(s => sessionRow(s, maxCost, filt)).join("")}
        </div>
      </div>`);
  }
  list.innerHTML = parts.join("");
}

function _kindBadge(s) {
  if (s.kind === "cron") return `<span class="pill kind-cron">⏰ cron</span>`;
  if (s.kind === "subagent") return `<span class="pill kind-sub">↳ subagent</span>`;
  return "";
}

function sessionRow(s, maxCost, filt) {
  const relWidth = ((s.cost || 0) / maxCost) * 100;
  const turnsBit = s.turns ? `<span class="pill">${s.turns} turni</span>` : "";
  const cacheBit = s.cacheRead ? `<span class="pill cache">${fmtNum(s.cacheRead)} cache</span>` : "";
  const startedDate = (s.startedAt || "").slice(0, 10);
  const overnightHint = startedDate && startedDate !== s.date
    ? `<span class="pill overnight">⏷ da ${escHtml(startedDate.slice(5))}</span>`
    : "";
  const orphanHint = s.kind === "subagent" && !s.parentSessionKey
    ? `<span class="pill orphan">↳ subagent orfano</span>` : "";

  const subs = (s.subagents || []).filter(sb =>
    _matchesFilter({topic: sb.topic, agent: sb.agent, modelDisplay: sb.modelDisplay}, filt));
  const subBlock = subs.length ? subagentsNested(s, subs) : "";

  return `
    <div class="session-card kind-${escHtml(s.kind || "main")}" data-session-id="${escHtml(s.id || "")}">
      <div class="session-card-head">
        <span class="agent-dot agent-${escHtml(s.agent)}"></span>
        <span class="session-agent">${escHtml(s.agent)}</span>
        ${_kindBadge(s)}
        ${typeof ownerChip === "function" ? ownerChip(s.owner) : ""}
        <span class="session-time">${escHtml(s.time || "")}<span class="muted">–${escHtml(s.endTime || "")}</span></span>
        <span class="session-model">${escHtml(s.modelDisplay || s.model || "")}</span>
        <span class="session-cost">${fmtUsd(s.cost)}</span>
      </div>
      <div class="session-cost-bar"><span style="width:${relWidth.toFixed(1)}%"></span></div>
      <div class="session-topic">${escHtml(s.topic || "")}</div>
      <div class="session-meta">
        <span class="pill">⏱ ${escHtml(s.duration || "0m")}</span>
        ${turnsBit}
        ${overnightHint}
        ${orphanHint}
        <span class="pill in">↓ ${fmtNum(s.tokensIn)}</span>
        <span class="pill out">↑ ${fmtNum(s.tokensOut)}</span>
        ${cacheBit}
      </div>
      ${subBlock}
    </div>`;
}

function subagentsNested(parent, subs) {
  const totalCost = subs.reduce((a, s) => a + (s.cost || 0), 0);
  return `
    <div class="nested-subs">
      <div class="nested-subs-title">
        <span>↳ ${subs.length} subagent${subs.length === 1 ? "" : "i"} chiamati</span>
        <span class="muted">${fmtUsd(totalCost)}</span>
      </div>
      ${subs.map(sb => nestedSubRow(parent, sb)).join("")}
    </div>`;
}

function nestedSubRow(parent, sb) {
  const key = (parent.id || parent.sessionKey || "?") + "|" + (sb.sessionKey || sb.childIdShort || "?");
  const open = _openSubs.has(key);
  const taskLine = (sb.task || sb.topic || "").split("\n")[0].trim();
  const pending = !sb.sessionId;
  return `
    <div class="nested-sub ${open ? "open" : ""} ${pending ? "pending" : ""}">
      <button class="nested-sub-head" data-toggle-sub="${escHtml(key)}" aria-expanded="${open}">
        <span class="agent-dot agent-${escHtml(sb.agent)}"></span>
        <span class="nested-sub-agent">${escHtml(sb.agent)}</span>
        <span class="nested-sub-from muted">← ${escHtml(parent.agent)}</span>
        <span class="nested-sub-task muted">${escHtml(taskLine.slice(0, 100) || "(in attesa)")}</span>
        <span class="nested-sub-cost">${fmtUsd(sb.cost || 0)}</span>
        <span class="day-chevron">${open ? "▾" : "▸"}</span>
      </button>
      ${open ? `
        <div class="nested-sub-body">
          <div class="sub-meta muted">
            ${escHtml(sb.modelDisplay || "?")} · ${sb.turns || 0} turni · ${escHtml(sb.duration || "—")}
            ${sb.status ? ` · stato: ${escHtml(sb.status)}` : ""}
          </div>
          <div class="session-meta">
            <span class="pill in">↓ ${fmtNum(sb.tokensIn || 0)}</span>
            <span class="pill out">↑ ${fmtNum(sb.tokensOut || 0)}</span>
            ${sb.cacheRead ? `<span class="pill cache">${fmtNum(sb.cacheRead)} cache</span>` : ""}
          </div>
          ${sb.task ? `
          <div class="topic-box">
            <span class="muted">task assegnato</span>
            <div class="topic-body">${escHtml(sb.task)}</div>
          </div>` : ""}
          ${sb.topic && sb.topic !== sb.task ? `
          <div class="topic-box">
            <span class="muted">esito / topic</span>
            <div class="topic-body">${escHtml(sb.topic)}</div>
          </div>` : ""}
        </div>` : ""}
    </div>`;
}

/* ───────── Live group at top ───────── */

function renderLiveGroup(d, filt) {
  const wrap = document.getElementById("liveGroup");
  if (!wrap) return;
  const agents = (d.currentByAgent || []).filter(a => _matchesFilter(a, filt));
  if (!agents.length) {
    wrap.hidden = true;
    wrap.innerHTML = "";
    return;
  }
  wrap.hidden = false;
  const loadedAt = (typeof getLoadedAt === "function") ? getLoadedAt() : Date.now();
  const ageSec = Math.max(0, Math.floor((Date.now() - loadedAt) / 1000));
  const ageLabel = ageSec < 2 ? "ora" : `${ageSec}s fa`;
  const distinctOwners = new Set(agents.map(a => (a.owner || {}).ownerKey || "?")).size;
  const ownerLabel = distinctOwners > 1 ? ` · ${distinctOwners} utenti` : "";
  wrap.innerHTML = `
    <div class="live-status">
      <span class="live-dot"></span>
      <span class="live-status-label">live · ${agents.length} session${agents.length === 1 ? "e" : "i"}${ownerLabel}</span>
      <span class="live-status-ago muted">aggiornato ${ageLabel}</span>
    </div>
    ${agents.map(a => agentLiveCard(a)).join("")}
  `;
}

/* ───────── Init: event delegation ───────── */

function initSessions() {
  const input = document.getElementById("sessionsFilter");
  input.addEventListener("input", () => {
    _sessionFilter = input.value;
    renderSessions();
  });

  // Delegate clicks for: day toggle, nested-sub toggle, AND live-card toggles
  // (the live cards still use data-toggle-agent / data-toggle-sub from live.js)
  document.getElementById("section-sessions").addEventListener("click", (e) => {
    const dayBtn = e.target.closest("[data-toggle-day]");
    if (dayBtn) {
      const date = dayBtn.dataset.toggleDay;
      if (_openDays.has(date)) _openDays.delete(date);
      else _openDays.add(date);
      renderSessions();
      return;
    }
    const subBtn = e.target.closest("[data-toggle-sub]");
    if (subBtn) {
      const k = subBtn.dataset.toggleSub;
      if (_openSubs.has(k)) _openSubs.delete(k);
      else _openSubs.add(k);
      renderSessions();
      return;
    }
    const liveBtn = e.target.closest("[data-toggle-agent]");
    if (liveBtn) {
      const a = liveBtn.dataset.toggleAgent;
      // delegate to live.js's own state (it has its own Set)
      if (typeof toggleLiveAgent === "function") toggleLiveAgent(a);
      renderSessions();
      return;
    }
  });
}
