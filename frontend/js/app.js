const isGitHubPages = window.location.hostname.endsWith("github.io");
const API = window.SMART_SHELF_API_BASE || "";
const POLL_MS = 10000;

let demandChart = null;
let selectedSku = null;
let runCounter = 0;
let demoModeActive = false;

// #region agent log
function debugLog(hypothesisId, location, message, data, runId = "initial") {
  fetch('http://127.0.0.1:7813/ingest/c9aad611-0274-4ffd-be88-42ac0dc92c89',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'f8be37'},body:JSON.stringify({sessionId:'f8be37',runId,hypothesisId,location,message,data,timestamp:Date.now()})}).catch(()=>{});
}
// #endregion

async function fetchJSON(path) {
  const url = `${API}${path}`;
  // #region agent log
  debugLog("H2", "app.js:fetchJSON", "request_start", { path, url }, `run-${runCounter}`);
  // #endregion
  const res = await fetch(url);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    // #region agent log
    debugLog("H2", "app.js:fetchJSON", "request_failed", { path, url, status: res.status, detail: err.detail || res.statusText }, `run-${runCounter}`);
    // #endregion
    throw new Error(err.detail || res.statusText);
  }
  // #region agent log
  debugLog("H4", "app.js:fetchJSON", "request_success", { path, status: res.status }, `run-${runCounter}`);
  // #endregion
  return res.json();
}

async function loadDemoData() {
  const res = await fetch("./demo-data.json");
  if (!res.ok) throw new Error("Demo data not found");
  return res.json();
}

function setConnectionStatus(live, text) {
  const el = document.getElementById("connectionStatus");
  el.classList.remove("status-pill--live", "status-pill--error");
  if (live) el.classList.add("status-pill--live");
  else el.classList.add("status-pill--error");
  el.lastChild.textContent = text;
}

function stockPercent(current, rop) {
  const max = Math.max(current, rop, 1);
  return Math.min(100, Math.round((current / max) * 100));
}

function renderSlots(slots) {
  const grid = document.getElementById("slotGrid");
  if (!slots.length) {
    grid.innerHTML = '<div class="loading-card">No slots configured.</div>';
    return;
  }

  grid.innerHTML = slots
    .map((s) => {
      const warn = s.needs_restock;
      const pct = stockPercent(s.current_stock, s.reorder_point);
      return `
        <article class="slot-card ${warn ? "slot-card--warn" : ""}">
          <div class="slot-card__header">
            <div>
              <div class="slot-card__slot">Slot ${s.slot_id}</div>
              <div class="slot-card__name">${s.name}</div>
            </div>
            <span class="slot-card__badge ${warn ? "slot-card__badge--warn" : "slot-card__badge--ok"}">
              ${warn ? "Restock" : "OK"}
            </span>
          </div>
          <div class="slot-card__stock">${s.current_stock} <span>units</span></div>
          <div class="slot-card__meta">
            <div>${s.sku}</div>
            <div>ROP: <strong>${s.reorder_point}</strong></div>
          </div>
          <div class="slot-card__bar" aria-hidden="true">
            <div class="slot-card__bar-fill" style="width: ${pct}%"></div>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderEvents(events) {
  const body = document.getElementById("eventBody");
  if (!events.length) {
    body.innerHTML =
      '<tr><td colspan="4" class="empty-cell">No events yet. Start <code>serial_listener.py</code> and trigger the IR sensors.</td></tr>';
    return;
  }

  body.innerHTML = events
    .map(
      (e) => `
      <tr>
        <td>${e.id}</td>
        <td>${e.slot_id}</td>
        <td>${e.sku}</td>
        <td>${e.time}</td>
      </tr>
    `
    )
    .join("");
}

function renderRecommendations(slots, config) {
  const container = document.getElementById("recommendations");
  const meta = document.getElementById("configMeta");
  const initial = config.initial_stock_per_slot?.["1"] ?? "N/A";
  meta.textContent = `Lead time = ${config.lead_time_days} days · Z-score = ${config.z_score} · Initial stock per slot = ${initial}`;

  container.innerHTML = slots
    .map((s) => {
      const open = s.needs_restock ? "rec-card--open" : "";
      const flagClass = s.needs_restock ? "rec-card__flag--warn" : "rec-card__flag--ok";
      const flagText = s.needs_restock ? "⚠ Restock needed" : "✓ Stock OK";
      return `
        <div class="rec-card ${open}" data-slot="${s.slot_id}">
          <button type="button" class="rec-card__toggle" aria-expanded="${s.needs_restock}">
            <span>Slot ${s.slot_id} — ${s.name} (${s.sku})</span>
            <span class="rec-card__flag ${flagClass}">${flagText}</span>
          </button>
          <div class="rec-card__body">
            <div>
              <h4>Inventory</h4>
              <ul class="rec-detail">
                <li><span class="rec-detail__label">Current stock</span><span class="rec-detail__value">${s.current_stock}</span></li>
                <li><span class="rec-detail__label">Removals (30d)</span><span class="rec-detail__value">${s.removals_in_window}</span></li>
                <li><span class="rec-detail__label">Predicted demand/day</span><span class="rec-detail__value">${s.predicted_daily_demand}</span></li>
                ${
                  s.needs_restock
                    ? `<li><span class="rec-detail__label">Shortfall</span><span class="rec-detail__value rec-detail__value--danger">${s.shortfall.toFixed(1)} units</span></li>`
                    : ""
                }
              </ul>
            </div>
            <div>
              <h4>Reorder point math</h4>
              <pre class="rec-math">${s.reorder_math}</pre>
            </div>
          </div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".rec-card__toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".rec-card");
      const open = card.classList.toggle("rec-card--open");
      btn.setAttribute("aria-expanded", open);
    });
  });
}

function renderSummary(health, slots) {
  const restockCount = slots.filter((s) => s.needs_restock).length;
  document.getElementById("statSlots").textContent = slots.length;
  document.getElementById("statRestock").textContent = restockCount;
  document.getElementById("statEvents").textContent = health.event_count;
  document.getElementById("statModel").textContent = health.model_loaded ? "Ready" : "Missing";
  document.getElementById("statModel").style.color = health.model_loaded ? "var(--ok)" : "var(--danger)";
}

function buildChartData(historical, predicted) {
  const histDates = historical.map((d) => d.date);
  const predDates = predicted.map((d) => d.date);
  const allDates = [...histDates, ...predDates];

  const histMap = Object.fromEntries(historical.map((d) => [d.date, d.sales]));
  const predMap = Object.fromEntries(predicted.map((d) => [d.date, d.sales]));

  return {
    labels: allDates,
    datasets: [
      {
        label: "Historical",
        data: allDates.map((d) => (d in histMap ? histMap[d] : null)),
        borderColor: "#8b9cb3",
        backgroundColor: "rgba(139, 156, 179, 0.1)",
        borderWidth: 2,
        pointRadius: 0,
        pointHitRadius: 8,
        tension: 0.3,
        spanGaps: false,
      },
      {
        label: "Predicted",
        data: allDates.map((d) => (d in predMap ? predMap[d] : null)),
        borderColor: "#3dd6c3",
        backgroundColor: "rgba(61, 214, 195, 0.08)",
        borderWidth: 2,
        borderDash: [6, 4],
        pointRadius: 3,
        pointBackgroundColor: "#3dd6c3",
        tension: 0.3,
        spanGaps: false,
      },
    ],
  };
}

function renderDemandChart(historical, predicted) {
  const ctx = document.getElementById("demandChart").getContext("2d");
  const data = buildChartData(historical, predicted);

  if (demandChart) {
    demandChart.data = data;
    demandChart.update("none");
    return;
  }

  demandChart = new Chart(ctx, {
    type: "line",
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: { color: "#8b9cb3", font: { family: "'DM Sans', sans-serif" } },
        },
      },
      scales: {
        x: {
          ticks: { color: "#8b9cb3", maxTicksLimit: 10, font: { size: 10 } },
          grid: { color: "rgba(42, 53, 68, 0.5)" },
        },
        y: {
          ticks: { color: "#8b9cb3", font: { size: 11 } },
          grid: { color: "rgba(42, 53, 68, 0.5)" },
          title: { display: true, text: "Units / day", color: "#8b9cb3" },
        },
      },
    },
  });
}

async function loadDemand(sku) {
  const caption = document.getElementById("chartMae");
  if (!sku) {
    caption.textContent = "Train the model to see demand charts.";
    return;
  }

  try {
    if (demoModeActive) {
      caption.textContent = "Demo mode: forecast API unavailable on GitHub Pages.";
      if (demandChart) {
        demandChart.destroy();
        demandChart = null;
      }
      return;
    }
    const data = await fetchJSON(`/api/demand/${encodeURIComponent(sku)}`);
    caption.textContent = data.mae != null ? `Validation MAE: ${data.mae.toFixed(2)} units/day` : "";
    renderDemandChart(data.historical, data.predicted);
  } catch (err) {
    caption.textContent = err.message;
    if (demandChart) {
      demandChart.destroy();
      demandChart = null;
    }
  }
}

async function populateSkuSelect(skus, metrics) {
  const select = document.getElementById("skuSelect");
  if (!skus.length) {
    select.innerHTML = '<option value="">No SKUs</option>';
    return;
  }

  select.innerHTML = skus.map((s) => `<option value="${s}">${s}</option>`).join("");
  selectedSku = skus[0];
  select.value = selectedSku;
  select.onchange = () => {
    selectedSku = select.value;
    loadDemand(selectedSku);
  };
  await loadDemand(selectedSku);
}

async function refresh() {
  runCounter += 1;
  // #region agent log
  debugLog("H1", "app.js:refresh", "refresh_start", { runCounter, apiBase: API, origin: window.location.origin }, `run-${runCounter}`);
  // #endregion
  try {
    if (demoModeActive) {
      return;
    }
    const [health, config, slotsData, eventsData, skuData] = await Promise.all([
      fetchJSON("/api/health"),
      fetchJSON("/api/config"),
      fetchJSON("/api/slots"),
      fetchJSON("/api/events?limit=100"),
      fetchJSON("/api/skus"),
    ]);

    const slots = slotsData.slots;
    renderSummary(health, slots);
    renderSlots(slots);
    renderEvents(eventsData.events);
    renderRecommendations(slots, config);

    if (!selectedSku && skuData.skus.length) {
      await populateSkuSelect(skuData.skus, skuData.metrics);
    }

    const now = new Date().toLocaleTimeString();
    setConnectionStatus(true, `Live · ${now}`);
  } catch (err) {
    if (isGitHubPages && !demoModeActive) {
      try {
        const demo = await loadDemoData();
        renderSummary(demo.health, demo.slots);
        renderSlots(demo.slots);
        renderEvents(demo.events);
        renderRecommendations(demo.slots, demo.config);
        if (demo.skus.length) {
          await populateSkuSelect(demo.skus, demo.metrics || {});
        }
        demoModeActive = true;
        // #region agent log
        debugLog(
          "H5",
          "app.js:refresh",
          "github_pages_demo_mode_enabled",
          { runCounter, originalError: String(err?.message || err), isGitHubPages, apiBase: API },
          `run-${runCounter}`
        );
        // #endregion
        setConnectionStatus(false, "Demo mode — backend unavailable on GitHub Pages");
        return;
      } catch (demoErr) {
        // #region agent log
        debugLog(
          "H6",
          "app.js:refresh",
          "demo_mode_failed",
          { runCounter, originalError: String(err?.message || err), demoError: String(demoErr?.message || demoErr) },
          `run-${runCounter}`
        );
        // #endregion
      }
    }
    // #region agent log
    debugLog("H3", "app.js:refresh", "refresh_error", { runCounter, error: String(err?.message || err) }, `run-${runCounter}`);
    // #endregion
    setConnectionStatus(false, `Offline — ${err.message}`);
  }
}

document.getElementById("refreshBtn").addEventListener("click", refresh);

refresh();
setInterval(refresh, POLL_MS);
