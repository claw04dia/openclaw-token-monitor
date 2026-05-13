/* sessions.js — sessions grouped by day, mobile-first. */

let _sessionFilter = "";
const _openDays = new Set();
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

function renderSessions() {
  const d = getData();
  const all = (d.sessions || []).slice();
  const filt = _sessionFilter.toLowerCase().trim();
  const filtered = filt
    ? all.filter(s =>
        (s.topic || "").toLowerCase().includes(filt) ||
        (s.agent || "").toLowerCase().includes(filt) ||
        (s.modelDisplay || "").toLowerCase().includes(filt))
    : all;

  const list = document.getElementById("sessionsList");
  const count = document.getElementById("sessionsCount");
  if (count) {
    count.textContent = filt
      ? `${filtered.length}/${all.length} sessioni`
      : `${all.length} sessioni · ${fmtUsd(d.stats?.totalCostFromSessionsUsd || 0)}`;
  }

  if (!filtered.length) {
    list.innerHTML = filt
      ? `<div class="empty muted">Nessun risultato per "${escHtml(filt)}"</div>`
      : '<div class="empty muted">Nessuna sessione</div>';
    return;
  }

  // Group by date (preserves builder order: newest first)
  const groups = new Map();
  for (const s of filtered) {
    if (!groups.has(s.date)) groups.set(s.date, []);
    groups.get(s.date).push(s);
  }

  // Auto-open most recent day on first render
  if (!_initialized) {
    const first = groups.keys().next().value;
    if (first) _openDays.add(first);
    _initialized = true;
  }

  // For relative bar widths across visible sessions
  const maxCost = Math.max(...filtered.map(s => s.cost || 0), 0.0001);

  const parts = [];
  for (const [date, items] of groups) {
    const dayCost = items.reduce((a, x) => a + (x.cost || 0), 0);
    const dayTokIn = items.reduce((a, x) => a + (x.tokensIn || 0), 0);
    const dayTokOut = items.reduce((a, x) => a + (x.tokensOut || 0), 0);
    const open = _openDays.has(date);
    parts.push(`
      <div class="day-group ${open ? "open" : ""}" data-date="${date}">
        <button class="day-head" data-toggle="${date}" aria-expanded="${open}">
          <span class="day-chevron">${open ? "▾" : "▸"}</span>
          <div class="day-titles">
            <span class="day-name">${fmtDateFriendly(date)}</span>
            <span class="day-iso muted">${escHtml(date)}</span>
          </div>
          <div class="day-numbers">
            <span class="day-cost">${fmtUsd(dayCost)}</span>
            <span class="day-meta muted">${items.length} sess · ${fmtNum(dayTokIn + dayTokOut)} tok</span>
          </div>
        </button>
        <div class="day-body" ${open ? "" : 'hidden'}>
          ${items.map(s => sessionRow(s, maxCost)).join("")}
        </div>
      </div>`);
  }
  list.innerHTML = parts.join("");
  // Click handler is attached once in initSessions() via delegation.
}

function sessionRow(s, maxCost) {
  const relWidth = ((s.cost || 0) / maxCost) * 100;
  const turnsBit = s.turns ? `<span class="pill">${s.turns} turni</span>` : "";
  const cacheBit = s.cacheRead ? `<span class="pill cache">${fmtNum(s.cacheRead)} cache</span>` : "";
  // If the session started on a different day (crossed midnight), show a hint
  const startedDate = (s.startedAt || "").slice(0, 10);
  const overnightHint = startedDate && startedDate !== s.date
    ? `<span class="pill overnight">⏷ da ${escHtml(startedDate.slice(5))}</span>`
    : "";
  return `
    <div class="session-card">
      <div class="session-card-head">
        <span class="agent-dot agent-${escHtml(s.agent)}"></span>
        <span class="session-agent">${escHtml(s.agent)}</span>
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
        <span class="pill in">↓ ${fmtNum(s.tokensIn)}</span>
        <span class="pill out">↑ ${fmtNum(s.tokensOut)}</span>
        ${cacheBit}
      </div>
    </div>`;
}

function initSessions() {
  const input = document.getElementById("sessionsFilter");
  input.addEventListener("input", () => {
    _sessionFilter = input.value;
    renderSessions();
  });

  // Event delegation: attach ONCE on the list, survives re-renders
  const list = document.getElementById("sessionsList");
  list.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-toggle]");
    if (!btn) return;
    const date = btn.dataset.toggle;
    if (_openDays.has(date)) _openDays.delete(date);
    else _openDays.add(date);
    renderSessions();
  });
}
