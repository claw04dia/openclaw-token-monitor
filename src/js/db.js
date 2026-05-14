/* db.js — authenticated fetch from the Token Monitor API.
 *
 * The backend validates the Telegram initData HMAC against the bot token,
 * so anyone without a valid Telegram session for this bot gets 401.
 *
 * Configure the API base URL by editing API_BASE below, or by setting
 * `?api=<url>` on the mini-app URL (persisted in localStorage).
 */

const API_KEY = "tm_api_base";
const PARAM_KEY = "api";

function _readApiBase() {
  const url = new URL(window.location.href);
  const fromQuery = url.searchParams.get(PARAM_KEY);
  if (fromQuery) {
    localStorage.setItem(API_KEY, fromQuery);
    url.searchParams.delete(PARAM_KEY);
    history.replaceState({}, "", url.toString());
    return fromQuery;
  }
  return localStorage.getItem(API_KEY) || ""; // empty = relative (same origin)
}

const API_BASE = _readApiBase();

const Store = { data: null, loading: false, loadedAt: 0, error: null };

async function loadData(force = false) {
  if (Store.loading) return Store.data;
  if (!force && Store.data && Date.now() - Store.loadedAt < 5000) return Store.data;
  Store.loading = true;
  Store.error = null;
  try {
    const tg = window.Telegram && window.Telegram.WebApp;
    const initData = tg ? tg.initData : "";
    if (!initData) {
      Store.error = "Apri questa app da Telegram (initData mancante)";
      console.warn(Store.error);
      return null;
    }
    const headers = { "Authorization": "tma " + initData };
    const url = (API_BASE || "") + "/api/data?_=" + Date.now();
    const resp = await fetch(url, { headers, cache: "no-store" });
    if (resp.status === 401) {
      Store.error = "Non autorizzato (initData scaduto?)";
      return null;
    }
    if (!resp.ok) {
      Store.error = "HTTP " + resp.status;
      return null;
    }
    Store.data = await resp.json();
    Store.loadedAt = Date.now();
    return Store.data;
  } catch (e) {
    Store.error = "Rete: " + e.message;
    console.error("loadData failed:", e);
    return null;
  } finally {
    Store.loading = false;
  }
}

function getData() { return Store.data || {}; }
function getError() { return Store.error; }
function getApiBase() { return API_BASE; }
function getLoadedAt() { return Store.loadedAt; }
function getViewer() { return (Store.data && Store.data.viewer) || null; }
function isAdmin() { return !!(Store.data && Store.data.viewer && Store.data.viewer.isAdmin); }
function isViewerScope() { return !!(Store.data && Store.data.scope === "viewer"); }

function escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}
function fmtUsd(v) {
  v = Number(v) || 0;
  if (v < 0.001) return "$" + v.toFixed(5);
  if (v < 0.01) return "$" + v.toFixed(4);
  if (v < 1) return "$" + v.toFixed(3);
  return "$" + v.toFixed(2);
}
function fmtNum(n) {
  n = Number(n) || 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}
function fmtAgo(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return "ora";
  if (diff < 3600) return Math.floor(diff / 60) + "min fa";
  if (diff < 86400) return Math.floor(diff / 3600) + "h fa";
  return Math.floor(diff / 86400) + "g fa";
}
