/* live.js — live card builder. The DOM rendering lives in sessions.js now;
 * this file just exports the card builders used at the top of the Sessions tab. */

const _openAgents = new Set();
const _openSubs = new Set();
let _liveInitialized = false;

function toggleLiveAgent(agent) {
  if (!_liveInitialized) { _liveInitialized = true; }
  if (_openAgents.has(agent)) _openAgents.delete(agent);
  else _openAgents.add(agent);
}

function ensureLiveDefaultOpen(firstAgent) {
  if (_liveInitialized || !firstAgent) return;
  _openAgents.add(firstAgent);
  _liveInitialized = true;
}

function fmtAgo2(iso) {
  if (!iso) return "—";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return Math.floor(diff) + "s fa";
  if (diff < 3600) return Math.floor(diff / 60) + "min fa";
  if (diff < 86400) return Math.floor(diff / 3600) + "h fa";
  return Math.floor(diff / 86400) + "g fa";
}

function fmtStartEnd(startedIso, endedIso) {
  if (!startedIso || !endedIso) return "—";
  const sd = startedIso.slice(5, 10), st = startedIso.slice(11, 16);
  const ed = endedIso.slice(5, 10), et = endedIso.slice(11, 16);
  if (sd === ed) return `${sd} · ${st} → ${et}`;
  return `${sd} ${st} → ${ed} ${et}`;
}

function ctxGauge(used, win) {
  if (!win) return "";
  const pct = (used / win) * 100;
  const display = Math.min(100, pct);
  const cls = pct >= 80 ? "warn" : "";
  const remaining = Math.max(0, win - used);
  return `
    <div class="ctx-block">
      <div class="ctx-block-row">
        <span class="ctx-block-label">Contesto della sessione</span>
        <span class="ctx-block-pct ${cls}">${pct.toFixed(0)}%</span>
      </div>
      <div class="ctx-gauge-bar"><span style="width:${display.toFixed(1)}%" class="${cls}"></span></div>
      <div class="ctx-block-foot muted">
        Picco massimo: ${fmtNum(used)} / ${fmtNum(win)} token · ${fmtNum(remaining)} liberi
      </div>
      <div class="ctx-block-hint muted">
        Quanto era pieno il prompt nel turno più carico. Quando si avvicina al 100% l'agente inizia a perdere memoria all'inizio della conversazione (è quello che OpenClaw mostra con <code>/context</code>).
      </div>
    </div>`;
}

function ownerChip(owner, opts) {
  if (!owner || !owner.ownerLabel) return "";
  opts = opts || {};
  const viewer = (typeof getViewer === "function") ? getViewer() : null;
  const isMe = viewer && String(viewer.id) === String(owner.ownerKey);
  const big = opts.big ? " big" : "";
  const cls = "owner-chip owner-" + (owner.ownerKind || "unknown") + (isMe ? " is-me" : "") + big;
  let label;
  if (isMe) {
    label = `✨ Tu (${owner.ownerKey})`;
  } else if (owner.ownerKind === "telegram") {
    label = `👤 utente ${owner.ownerKey}`;
  } else {
    label = owner.ownerLabel;
  }
  return `<span class="${cls}" title="${escHtml(owner.ownerKey || "")}">${escHtml(label)}</span>`;
}

function agentLiveCard(s) {
  // Live cards are keyed by sessionId (not agent) so two users on the same
  // agent expand independently.
  const key = s.sessionId || s.sessionKey || s.agent;
  const open = _openAgents.has(key);
  const used = s.peakSingleCall || 0;
  const win = s.contextWindow || 0;

  const parentCost = s.parentCost ?? s.cost ?? 0;
  const subCost = s.subagentsCost || 0;
  const totalCost = s.totalCost ?? (parentCost + subCost);
  const subCount = (s.subagents || []).length;

  const isMine = (() => {
    const v = (typeof getViewer === "function") ? getViewer() : null;
    return v && s.owner && String(v.id) === String(s.owner.ownerKey);
  })();

  return `
    <div class="live-card ${open ? "open" : ""} ${isMine ? "mine" : "other"}" data-sid="${escHtml(key)}">
      <div class="live-owner-banner">
        ${ownerChip(s.owner, {big: true})}
        <span class="live-owner-meta muted">${escHtml(s.sessionId || "").slice(0, 8)}</span>
      </div>
      <button class="live-head" data-toggle-agent="${escHtml(key)}" aria-expanded="${open}">
        <span class="agent-dot agent-${escHtml(s.agent)}"></span>
        <div class="live-titles">
          <span class="live-agent">${escHtml(s.agent)}</span>
          <span class="live-sub muted">${escHtml(s.modelDisplay)} · ${s.turns} turni · ${escHtml(s.duration)}</span>
        </div>
        <div class="live-numbers">
          <span class="live-cost">${fmtUsd(totalCost)}</span>
          <span class="live-ago muted">${fmtAgo2(s.ended)}</span>
        </div>
        <span class="day-chevron">${open ? "▾" : "▸"}</span>
      </button>
      ${open ? `
      <div class="live-body">
        <div class="live-when muted">${escHtml(fmtStartEnd(s.started, s.ended))}</div>

        <div class="cost-breakdown">
          <div class="cost-row total">
            <span class="cost-label">Costo totale sessione</span>
            <span class="cost-val">${fmtUsd(totalCost)}</span>
          </div>
          <div class="cost-row">
            <span class="cost-label">
              <span class="agent-dot agent-${escHtml(s.agent)}"></span>
              ${escHtml(s.agent)} (main)
            </span>
            <span class="cost-val">${fmtUsd(parentCost)}</span>
          </div>
          <div class="cost-row ${subCount ? "" : "muted"}">
            <span class="cost-label">↳ subagenti${subCount ? ` (${subCount})` : ""}</span>
            <span class="cost-val">${fmtUsd(subCost)}</span>
          </div>
        </div>

        ${ctxGauge(used, win)}

        <div class="token-grid">
          <div class="token-tile in">
            <span class="token-tile-label">Input nuovo</span>
            <span class="token-tile-val">${fmtNum(s.tokensIn)}</span>
          </div>
          <div class="token-tile out">
            <span class="token-tile-label">Output</span>
            <span class="token-tile-val">${fmtNum(s.tokensOut)}</span>
          </div>
          <div class="token-tile cache">
            <span class="token-tile-label">Cache (sconto)</span>
            <span class="token-tile-val">${fmtNum(s.cacheRead)}</span>
          </div>
        </div>

        <div class="topic-box">
          <span class="muted">di cosa parla</span>
          <div class="topic-body">${escHtml(s.topic)}</div>
        </div>

        ${subagentsBlock(s)}

        <div class="turns-list">
          <div class="turns-list-title">Ultimi turni (più recenti in alto)</div>
          ${(s.lastTurns || []).slice().reverse().map(t => turnRow(t)).join("")}
        </div>
      </div>` : ""}
    </div>`;
}

function subagentsBlock(parent) {
  const subs = parent.subagents || [];
  if (!subs.length) {
    return `
      <div class="subagents-block">
        <div class="subagents-title">Subagenti chiamati in questa sessione</div>
        <div class="subagents-empty muted">Nessuno</div>
      </div>`;
  }
  const subCost = parent.subagentsCost || 0;
  const subTurns = parent.subagentsTurns || 0;
  return `
    <div class="subagents-block">
      <div class="subagents-title-row">
        <span class="subagents-title">Subagenti · ${subs.length}</span>
        <span class="subagents-stats muted">${subTurns} turni · <strong>${fmtUsd(subCost)}</strong></span>
      </div>
      <div class="subagents-list">
        ${subs.map(sb => subagentCard(parent.agent, sb)).join("")}
      </div>
    </div>`;
}

function subagentCard(parentAgent, sb) {
  const key = parentAgent + "|" + (sb.sessionKey || sb.sessionId || sb.childIdShort);
  const open = _openSubs.has(key);
  const taskLine = (sb.task || sb.topic || "").split("\n")[0].trim();
  const pending = !sb.sessionId;
  return `
    <div class="sub-card ${open ? "open" : ""} ${pending ? "pending" : ""}">
      <button class="sub-head" data-toggle-sub="${escHtml(key)}" aria-expanded="${open}">
        <span class="agent-dot agent-${escHtml(sb.agent)}"></span>
        <div class="sub-titles">
          <span class="sub-agent">${escHtml(sb.agent)}${sb.childIdShort ? ` <span class="muted">${escHtml(sb.childIdShort)}</span>` : ""}</span>
          <span class="sub-task muted">${escHtml(taskLine.slice(0, 120) || "(in attesa)")}</span>
        </div>
        <div class="sub-numbers">
          <span class="sub-cost">${fmtUsd(sb.cost || 0)}</span>
          <span class="sub-ago muted">${fmtAgo2(sb.spawnedAt || sb.started)}</span>
        </div>
        <span class="day-chevron">${open ? "▾" : "▸"}</span>
      </button>
      ${open ? `
      <div class="sub-body">
        <div class="sub-meta muted">
          ${escHtml(sb.modelDisplay || "?")} · ${sb.turns} turni · ${escHtml(sb.duration || "—")}
          ${sb.status ? ` · stato: ${escHtml(sb.status)}` : ""}
        </div>
        <div class="token-grid">
          <div class="token-tile in">
            <span class="token-tile-label">Input</span>
            <span class="token-tile-val">${fmtNum(sb.tokensIn || 0)}</span>
          </div>
          <div class="token-tile out">
            <span class="token-tile-label">Output</span>
            <span class="token-tile-val">${fmtNum(sb.tokensOut || 0)}</span>
          </div>
          <div class="token-tile cache">
            <span class="token-tile-label">Cache</span>
            <span class="token-tile-val">${fmtNum(sb.cacheRead || 0)}</span>
          </div>
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

function turnRow(t) {
  const prompt = (t.promptPreview || "").trim();
  const reply = (t.replyPreview || "").trim();
  return `
    <div class="turn-row">
      <div class="turn-head">
        <span class="turn-time">${escHtml(t.ts.slice(11, 16))}</span>
        <span class="turn-tokens">
          <span class="pill in">↓ ${fmtNum(t.tokensIn)}</span>
          <span class="pill out">↑ ${fmtNum(t.tokensOut)}</span>
          ${t.cacheRead ? `<span class="pill cache">⟳ ${fmtNum(t.cacheRead)}</span>` : ""}
        </span>
      </div>
      ${prompt ? `<div class="turn-prompt"><span class="muted">tu →</span> ${escHtml(prompt)}</div>` : ""}
      ${reply ? `<div class="turn-reply"><span class="muted">agente →</span> ${escHtml(reply)}</div>` : ""}
    </div>`;
}
