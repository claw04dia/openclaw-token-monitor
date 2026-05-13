/* prices.js — read-only listing. Editing now happens server-side. */

function renderPrices() {
  const d = getData();
  const prices = (d.prices || []).slice();
  prices.sort((a, b) => (a.outputPrice || 0) - (b.outputPrice || 0));
  const wrap = document.getElementById("pricesList");
  if (!prices.length) {
    wrap.innerHTML = '<div class="empty muted">Nessun pricing</div>';
    return;
  }
  wrap.innerHTML = prices.map(p => `
    <div class="price-row">
      <div>
        <div class="price-name">${escHtml(p.model)}</div>
        <div class="price-id">${escHtml(p.id || "")}</div>
      </div>
      <div class="price-val">
        <span class="label">in</span>
        ${Number(p.inputPrice || 0).toFixed(2)}
      </div>
      <div class="price-val">
        <span class="label">out</span>
        ${Number(p.outputPrice || 0).toFixed(2)}
      </div>
    </div>`).join("");
}
