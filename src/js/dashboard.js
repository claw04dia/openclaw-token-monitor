/* dashboard.js — renders the at-a-glance home view. */

function renderDashboard() {
  const d = getData();
  const stats = d.stats || {};
  const agents = d.agentBreakdown || [];
  const bloated = d.bloated || [];
  const daily = d.daily || [];

  // Header date + updatedAt
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById("todayDate").textContent = today;
  document.getElementById("updatedAgo").textContent = "agg. " + fmtAgo(d.updatedAt);

  // Budget card — uses REAL OpenRouter today value (override from stats)
  const todayCost = Number(stats.todayCostUsd ?? 0);
  const budget = Number(stats.dailyBudgetUsd ?? 1);
  const pct = budget > 0 ? Math.min(100, (todayCost / budget) * 100) : 0;
  document.getElementById("todayCost").textContent = fmtUsd(todayCost);
  document.getElementById("dailyBudget").textContent = fmtUsd(budget);
  const fill = document.getElementById("budgetFill");
  fill.style.width = pct + "%";
  fill.classList.toggle("warn", pct >= 50 && pct < 90);
  fill.classList.toggle("danger", pct >= 90);
  document.getElementById("todayPct").textContent =
    pct >= 100 ? "OLTRE BUDGET (" + pct.toFixed(0) + "%)" : pct.toFixed(0) + "% budget";
  document.getElementById("todayTokens").textContent = "dati OpenRouter";

  // Stats trio uses REAL OpenRouter aggregates
  document.getElementById("week-cost").textContent =
    fmtUsd(stats.realWeekCostUsd ?? 0);
  document.getElementById("month-cost").textContent =
    fmtUsd(stats.realMonthCostUsd ?? 0);
  document.getElementById("all-cost").textContent =
    fmtUsd(stats.realLifetimeCostUsd ?? 0);
  if (stats.remainingCreditsUsd != null) {
    document.getElementById("all-sub").textContent =
      "resta " + fmtUsd(stats.remainingCreditsUsd);
  }

  // Agent breakdown (real cost from sessions)
  const totalAgentCost = agents.reduce((a, x) => a + (x.cost || 0), 0) || 1e-9;
  const wrap = document.getElementById("agentBars");
  if (!agents.length) {
    wrap.innerHTML = '<div class="empty muted">Nessun dato</div>';
  } else {
    wrap.innerHTML = agents.map(a => {
      const p = (a.cost / totalAgentCost) * 100;
      const h = Math.floor((a.minutes || 0) / 60);
      const m = (a.minutes || 0) % 60;
      const dur = h ? `${h}h${String(m).padStart(2, "0")}` : `${m}m`;
      const model = a.topModelDisplay || a.topModel || "—";
      return `
        <div class="agent-row">
          <span class="agent-name">${escHtml(a.agent)}</span>
          <span class="agent-bar"><span class="agent-bar-fill" style="width:${p.toFixed(1)}%"></span></span>
          <span class="agent-cost">${fmtUsd(a.cost)}</span>
          <span class="agent-meta">${a.sessions} sess · ${dur} · ${escHtml(model)} · ${p.toFixed(0)}%</span>
        </div>`;
    }).join("");
  }

  // Bloated sessions (top 5 by real cost)
  const blWrap = document.getElementById("bloatList");
  const top = bloated.slice(0, 5);
  if (!top.length) {
    blWrap.innerHTML = '<div class="empty muted">Nessuna sessione</div>';
  } else {
    blWrap.innerHTML = top.map(s => `
      <div class="bloat-row">
        <span class="bloat-icon">🔥</span>
        <div class="bloat-head">
          <span class="bloat-dur">${escHtml(s.duration || "")}</span>
          <span class="bloat-agent">${escHtml(s.agent)}</span>
          <span class="bloat-date">${escHtml(s.date.slice(5))} ${escHtml(s.time || "")}</span>
        </div>
        <span class="bloat-cost">${fmtUsd(s.cost)}</span>
        <span class="bloat-topic">${escHtml(s.topic)}</span>
      </div>`).join("");
  }

  // Trend chart: last 14 days REAL costs
  const last14 = daily.slice(-14);
  const trendWrap = document.getElementById("trendChart");
  if (!last14.length) {
    trendWrap.innerHTML = '<div class="empty muted">Nessun dato</div>';
  } else {
    const max = Math.max(...last14.map(x => x.cost || 0), 0.0001);
    trendWrap.innerHTML = last14.map(x => {
      const h = ((x.cost || 0) / max) * 100;
      const tip = `${fmtUsd(x.cost)} · ${x.sessions}s`;
      return `
        <div class="trend-day">
          <div class="trend-bar-wrap">
            <div class="trend-bar" style="height:${h.toFixed(1)}%" data-tip="${tip}"></div>
          </div>
          <span>${x.date.slice(5)}</span>
        </div>`;
    }).join("");
  }
}
