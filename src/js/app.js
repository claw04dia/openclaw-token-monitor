/* app.js — boot, navigation, refresh. */

(async function () {
  if (window.Telegram && window.Telegram.WebApp) {
    const tg = window.Telegram.WebApp;
    tg.ready();
    tg.expand();
  }

  const LIVE_POLL_MS = 10000;
  let _livePollTimer = null;
  let _currentSection = null;

  async function pollLiveOnce() {
    if (document.hidden) return;
    const data = await loadData(true);
    if (data) renderLive();
  }

  function startLivePolling() {
    if (_livePollTimer) return;
    _livePollTimer = setInterval(pollLiveOnce, LIVE_POLL_MS);
  }

  function stopLivePolling() {
    if (!_livePollTimer) return;
    clearInterval(_livePollTimer);
    _livePollTimer = null;
  }

  document.addEventListener("visibilitychange", () => {
    if (_currentSection !== "live") return;
    if (document.hidden) stopLivePolling();
    else { startLivePolling(); pollLiveOnce(); }
  });

  function navigate(section) {
    document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));
    document.querySelectorAll(".nav-link").forEach(l => l.classList.remove("active"));
    const target = document.getElementById("section-" + section);
    if (target) target.classList.add("active");
    const link = document.querySelector(`.nav-link[data-section="${section}"]`);
    if (link) link.classList.add("active");
    if (section === "dashboard") renderDashboard();
    if (section === "live") renderLive();
    if (section === "sessions") renderSessions();
    if (section === "cron") renderCron();
    if (section === "prices") renderPrices();
    _currentSection = section;
    if (section === "live") { pollLiveOnce(); startLivePolling(); }
    else stopLivePolling();
    window.scrollTo(0, 0);
  }

  document.querySelectorAll(".nav-link").forEach(btn => {
    btn.addEventListener("click", () => navigate(btn.dataset.section));
  });

  // Chart.js no longer needed — trend rendered as CSS bars in dashboard.js

  initSessions();

  function showError(msg) {
    document.querySelectorAll(".empty").forEach(el => {
      el.textContent = msg;
      el.classList.add("error");
    });
  }

  async function refresh() {
    const btn = document.getElementById("refreshBtn");
    btn.classList.add("spinning");
    const data = await loadData(true);
    btn.classList.remove("spinning");
    if (!data) {
      showError("⚠ " + (getError() || "Impossibile caricare i dati"));
      btn.textContent = "⚠";
      setTimeout(() => (btn.textContent = "↻"), 1500);
      return;
    }
    renderDashboard();
    renderLive();
    renderSessions();
    renderCron(true);
    renderPrices();
  }
  document.getElementById("refreshBtn").addEventListener("click", refresh);

  // Initial load
  const data = await loadData(true);
  if (!data) showError("⚠ " + (getError() || "Impossibile caricare i dati"));
  navigate("dashboard");
})();
