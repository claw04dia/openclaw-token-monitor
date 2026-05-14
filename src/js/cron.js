/* cron.js — read-only view of ~/.openclaw/cron/jobs.json + crontab utente. */

const _cronStore = { data: null, loadedAt: 0, loading: false, error: null };
const _sysCronStore = { data: null, loadedAt: 0, loading: false, error: null };
const _cronOpen = new Set();

async function loadCron(force = false) {
  if (_cronStore.loading) return _cronStore.data;
  if (!force && _cronStore.data && Date.now() - _cronStore.loadedAt < 5000) {
    return _cronStore.data;
  }
  _cronStore.loading = true;
  _cronStore.error = null;
  try {
    const tg = window.Telegram && window.Telegram.WebApp;
    const initData = tg ? tg.initData : "";
    if (!initData) { _cronStore.error = "initData mancante"; return null; }
    const url = (getApiBase() || "") + "/api/cron?_=" + Date.now();
    const resp = await fetch(url, {
      headers: { "Authorization": "tma " + initData },
      cache: "no-store",
    });
    if (!resp.ok) { _cronStore.error = "HTTP " + resp.status; return null; }
    _cronStore.data = await resp.json();
    _cronStore.loadedAt = Date.now();
    return _cronStore.data;
  } catch (e) {
    _cronStore.error = "Rete: " + e.message;
    return null;
  } finally {
    _cronStore.loading = false;
  }
}

async function loadSysCron(force = false) {
  if (_sysCronStore.loading) return _sysCronStore.data;
  if (!force && _sysCronStore.data && Date.now() - _sysCronStore.loadedAt < 5000) {
    return _sysCronStore.data;
  }
  _sysCronStore.loading = true;
  _sysCronStore.error = null;
  try {
    const tg = window.Telegram && window.Telegram.WebApp;
    const initData = tg ? tg.initData : "";
    if (!initData) { _sysCronStore.error = "initData mancante"; return null; }
    const url = (getApiBase() || "") + "/api/system-cron?_=" + Date.now();
    const resp = await fetch(url, {
      headers: { "Authorization": "tma " + initData },
      cache: "no-store",
    });
    if (!resp.ok) { _sysCronStore.error = "HTTP " + resp.status; return null; }
    _sysCronStore.data = await resp.json();
    _sysCronStore.loadedAt = Date.now();
    return _sysCronStore.data;
  } catch (e) {
    _sysCronStore.error = "Rete: " + e.message;
    return null;
  } finally {
    _sysCronStore.loading = false;
  }
}

/* ───────── Cron expression humanizer (Italian, best-effort) ───────── */

const _DOW = ["dom", "lun", "mar", "mer", "gio", "ven", "sab"];
const _MONTHS = ["gen","feb","mar","apr","mag","giu","lug","ago","set","ott","nov","dic"];

function _humanField(value, kind) {
  if (value === "*") return null;
  if (value.startsWith("*/")) return `ogni ${value.slice(2)} ${kind}`;
  if (value.includes(",")) return value.split(",").map(v => _humanField(v, kind)).filter(Boolean).join(", ");
  if (value.includes("-")) {
    const [a, b] = value.split("-");
    return `da ${_humanField(a, kind)} a ${_humanField(b, kind)}`;
  }
  if (kind === "dow") return _DOW[Number(value)] || value;
  if (kind === "month") return _MONTHS[Number(value) - 1] || value;
  return value;
}

function humanizeCron(expr, tz) {
  if (!expr) return "—";
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return expr;
  const [min, hour, dom, mon, dow] = parts;
  const pad = (n) => String(n).padStart(2, "0");

  let timeStr;
  if (min.startsWith("*/") && hour === "*") timeStr = `ogni ${min.slice(2)} minuti`;
  else if (min === "*" && hour === "*") timeStr = "ogni minuto";
  else if (/^\d+$/.test(min) && /^\d+$/.test(hour)) timeStr = `alle ${pad(hour)}:${pad(min)}`;
  else if (/^\d+$/.test(hour) && min === "0") timeStr = `alle ${pad(hour)}:00`;
  else timeStr = `min ${min}, h ${hour}`;

  const bits = [timeStr];
  if (dow !== "*") {
    const h = _humanField(dow, "dow");
    bits.push(dow === "0" || dow === "7" ? "la domenica" : `il ${h}`);
  } else if (dom === "*" && mon === "*") {
    bits.push("ogni giorno");
  }
  if (dom !== "*") bits.push(`giorno ${dom}`);
  if (mon !== "*") bits.push(_humanField(mon, "month"));
  return bits.join(" · ") + (tz && tz !== "UTC" ? ` (${tz})` : " UTC");
}

/* ───────── Renderer ───────── */

function _agentColor(name) {
  if (!name) return "";
  const map = {
    "weekly-memory-promotion": "agent-main",
    "morning-briefing": "agent-leo",
    "daily-session-digest": "agent-sofia",
    "daily-audit-reconstruct": "agent-argo",
  };
  return map[name] || "";
}

function _deliveryBadge(d) {
  if (!d) return "";
  const mode = d.mode || "none";
  if (mode === "none") return `<span class="cron-pill mute">silente</span>`;
  if (mode === "announce" && d.channel === "telegram") {
    return `<span class="cron-pill telegram">📨 telegram</span>`;
  }
  return `<span class="cron-pill">${escHtml(mode)}/${escHtml(d.channel || "")}</span>`;
}

async function renderCron(force = false) {
  const list = document.getElementById("cronList");
  const count = document.getElementById("cronCount");
  const updated = document.getElementById("cronUpdated");

  if (!_cronStore.data || force) {
    list.innerHTML = '<div class="empty muted">Caricamento…</div>';
    await loadCron(force);
  }
  const d = _cronStore.data;
  if (!d) {
    list.innerHTML = `<div class="empty error">⚠ ${escHtml(_cronStore.error || "Errore")}</div>`;
    return;
  }
  if (d.error) {
    list.innerHTML = `<div class="empty error">⚠ ${escHtml(d.error)}</div>`;
    return;
  }

  const jobs = d.jobs || [];
  const enabled = jobs.filter(j => j.enabled).length;
  if (count) count.textContent = `${jobs.length} job · ${enabled} attivi`;
  if (updated && d.updatedAt) {
    updated.textContent = "jobs.json " + fmtAgo(new Date(d.updatedAt * 1000).toISOString());
  }

  if (!jobs.length) {
    list.innerHTML = '<div class="empty muted">Nessun job</div>';
    return;
  }

  list.innerHTML = jobs.map(j => {
    const open = _cronOpen.has(j.id);
    const tools = (j.payload.toolsAllow || []).join(", ");
    const dot = _agentColor(j.name);
    return `
      <div class="cron-card ${j.enabled ? "" : "disabled"} ${open ? "open" : ""}" data-id="${escHtml(j.id)}">
        <button class="cron-head" data-toggle="${escHtml(j.id)}" aria-expanded="${open}">
          <span class="cron-dot ${dot}"></span>
          <div class="cron-titles">
            <div class="cron-name-row">
              <span class="cron-name">${escHtml(j.name || j.id)}</span>
              ${j.enabled ? "" : '<span class="cron-pill off">disabilitato</span>'}
            </div>
            <span class="cron-when">${escHtml(humanizeCron(j.schedule.expr, j.schedule.tz))}</span>
            <code class="cron-expr">${escHtml(j.schedule.expr || "")}</code>
          </div>
          <div class="cron-meta">
            ${_deliveryBadge(j.delivery)}
            <span class="cron-chevron">${open ? "▾" : "▸"}</span>
          </div>
        </button>
        ${open ? `
          <div class="cron-body">
            ${j.description ? `<div class="cron-desc">${escHtml(j.description)}</div>` : ""}
            <div class="cron-kv">
              <span class="cron-k">Sessione</span><span>${escHtml(j.sessionTarget || "—")}</span>
              <span class="cron-k">Wake</span><span>${escHtml(j.wakeMode || "—")}</span>
              <span class="cron-k">Payload</span><span>${escHtml(j.payload.kind || "—")}${j.payload.thinking ? ` · thinking=${escHtml(j.payload.thinking)}` : ""}</span>
              ${j.payload.model ? `<span class="cron-k">Modello</span><span><code>${escHtml(j.payload.model)}</code></span>` : ""}
              ${tools ? `<span class="cron-k">Tools</span><span><code>${escHtml(tools)}</code></span>` : ""}
              ${j.delivery.to ? `<span class="cron-k">Destinatario</span><span><code>${escHtml(j.delivery.to)}</code></span>` : ""}
            </div>
            ${j.payload.message ? `
              <details class="cron-msg">
                <summary>Messaggio (${j.payload.messageLength} char)</summary>
                <pre>${escHtml(j.payload.message)}</pre>
              </details>` : ""}
          </div>` : ""}
      </div>`;
  }).join("");

  list.querySelectorAll(".cron-head").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.toggle;
      if (_cronOpen.has(id)) _cronOpen.delete(id); else _cronOpen.add(id);
      renderCron(false);
    });
  });

  renderSysCron(force);
}

/* ───────── System cron renderer ───────── */

const _CRON_SHORTCUT_LABELS = {
  "@reboot": "al boot",
  "@hourly": "ogni ora",
  "@daily": "ogni giorno (00:00)",
  "@midnight": "ogni mezzanotte",
  "@weekly": "ogni settimana (dom 00:00)",
  "@monthly": "ogni mese (giorno 1, 00:00)",
  "@yearly": "ogni anno (1 gen, 00:00)",
  "@annually": "ogni anno (1 gen, 00:00)",
};

function _humanizeSys(expr, exprStandard) {
  if (expr && expr.startsWith("@")) {
    return _CRON_SHORTCUT_LABELS[expr] || expr;
  }
  return humanizeCron(exprStandard || expr, "");
}

function _shortCommand(cmd) {
  if (!cmd) return "";
  // strip trailing redirects to focus on the actual script
  const noRedir = cmd.replace(/\s*>\s*\S+(\s+2>&1)?\s*$/, "").trim();
  return noRedir.length > 80 ? noRedir.slice(0, 78) + "…" : noRedir;
}

async function renderSysCron(force = false) {
  const list = document.getElementById("sysCronList");
  const count = document.getElementById("sysCronCount");
  const src = document.getElementById("sysCronSource");
  if (!list) return;

  if (!_sysCronStore.data || force) {
    list.innerHTML = '<div class="empty muted">Caricamento…</div>';
    await loadSysCron(force);
  }
  const d = _sysCronStore.data;
  if (!d) {
    list.innerHTML = `<div class="empty error">⚠ ${escHtml(_sysCronStore.error || "Errore")}</div>`;
    return;
  }
  if (d.error) {
    list.innerHTML = `<div class="empty error">⚠ ${escHtml(d.error)}</div>`;
    return;
  }

  const entries = d.entries || [];
  if (count) count.textContent = `${entries.length} entrate`;
  if (src) src.textContent = d.source || "";

  if (!entries.length) {
    list.innerHTML = '<div class="empty muted">crontab utente vuoto</div>';
    return;
  }

  list.innerHTML = entries.map(e => {
    const id = "sys-" + e.line;
    const open = _cronOpen.has(id);
    return `
      <div class="cron-card ${open ? "open" : ""}">
        <button class="cron-head" data-toggle="${id}" aria-expanded="${open}">
          <span class="cron-dot"></span>
          <div class="cron-titles">
            <div class="cron-name-row">
              <span class="cron-name">${escHtml(_shortCommand(e.command))}</span>
            </div>
            <span class="cron-when">${escHtml(_humanizeSys(e.expr, e.exprStandard))}</span>
            <code class="cron-expr">${escHtml(e.expr)}</code>
          </div>
          <div class="cron-meta">
            <span class="cron-pill mute">L${e.line}</span>
            <span class="cron-chevron">${open ? "▾" : "▸"}</span>
          </div>
        </button>
        ${open ? `
          <div class="cron-body">
            <div class="cron-kv">
              <span class="cron-k">Comando</span><span><code>${escHtml(e.command)}</code></span>
              <span class="cron-k">Riga raw</span><span><code>${escHtml(e.raw)}</code></span>
            </div>
          </div>` : ""}
      </div>`;
  }).join("");

  list.querySelectorAll(".cron-head").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.toggle;
      if (_cronOpen.has(id)) _cronOpen.delete(id); else _cronOpen.add(id);
      renderSysCron(false);
    });
  });
}
