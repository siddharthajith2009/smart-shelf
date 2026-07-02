const isGitHubPages = window.location.hostname.endsWith("github.io");
const API = window.SMART_SHELF_API_BASE || "";
const POLL_MS = 10000;

let demandChart = null;
let selectedSku = null;
let demoModeActive = false;

async function fetchJSON(path) {
  const url = `${API}${path}`;
  const res = await fetch(url);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

async function loadDemoData() {
  const res = await fetch("./demo-data.json");
  if (!res.ok) throw new Error("Demo data not found");
  return res.json();
}

function setConnectionStatus(live, text) {
  const el = document.getElementById("connectionStatus");
  const now = new Date().toLocaleTimeString();
  el.classList.remove("status-pill--live", "status-pill--error");
  if (live) el.classList.add("status-pill--live");
  else el.classList.add("status-pill--error");
  el.lastChild.textContent = text;
  const updatedChip = document.getElementById("lastUpdatedChip");
  if (updatedChip) updatedChip.textContent = `Updated: ${now}`;
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
      const statusLabel = warn ? (s.shortfall > 20 ? "Critical" : "Low") : "Stable";
      const statusClass = warn
        ? s.shortfall > 20
          ? "inventory-item__status--critical"
          : "inventory-item__status--warn"
        : "inventory-item__status--ok";
      return `
        <article class="inventory-item inventory-row">
          <span class="inventory-item__slot">Slot ${s.slot_id}</span>
          <span class="inventory-item__product">${s.name}</span>
          <span class="inventory-item__sku">${s.sku}</span>
          <span class="inventory-item__units">${s.current_stock}</span>
          <span class="inventory-item__rop">${s.reorder_point}</span>
          <span class="stock-bar" aria-hidden="true">
            <span class="stock-bar__fill ${warn ? "stock-bar__fill--warn" : ""}" style="width:${pct}%"></span>
          </span>
          <span class="inventory-item__status ${statusClass}">${statusLabel}</span>
          <span class="inventory-item__action">Monitor</span>
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
  const modelReady = health.model_loaded;
  document.getElementById("statSlots").textContent = slots.length;
  document.getElementById("statRestock").textContent = restockCount;
  document.getElementById("statEvents").textContent = health.event_count;
  const modelEl = document.getElementById("statModel");
  modelEl.textContent = modelReady ? "Ready" : "Missing";
  modelEl.classList.toggle("summary-stat__value--ok", modelReady);
  modelEl.classList.toggle("summary-stat__value--danger", !modelReady);
  const modelChip = document.getElementById("modelReadyChip");
  if (modelChip) {
    modelChip.textContent = `Model: ${modelReady ? "Ready" : "Missing"}`;
    modelChip.classList.toggle("meta-chip--ok", modelReady);
    modelChip.classList.toggle("meta-chip--danger", !modelReady);
  }
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
        borderColor: "#94A3B8",
        backgroundColor: "rgba(148, 163, 184, 0.1)",
        borderWidth: 2,
        pointRadius: 0,
        pointHitRadius: 8,
        tension: 0.3,
        spanGaps: false,
      },
      {
        label: "Predicted",
        data: allDates.map((d) => (d in predMap ? predMap[d] : null)),
        borderColor: "#22D3EE",
        backgroundColor: "rgba(34, 211, 238, 0.12)",
        borderWidth: 2,
        borderDash: [6, 4],
        pointRadius: 3,
        pointBackgroundColor: "#2DD4BF",
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
          labels: { color: "#94A3B8", font: { family: "Inter, sans-serif" } },
        },
        tooltip: {
          backgroundColor: "rgba(15, 23, 42, 0.96)",
          borderColor: "rgba(148, 163, 184, 0.24)",
          borderWidth: 1,
          titleColor: "#F8FAFC",
          bodyColor: "#E2E8F0",
          displayColors: true,
        },
      },
      scales: {
        x: {
          ticks: { color: "#94A3B8", maxTicksLimit: 10, font: { size: 10 } },
          grid: { color: "rgba(100, 116, 139, 0.24)" },
        },
        y: {
          ticks: { color: "#94A3B8", font: { size: 11 } },
          grid: { color: "rgba(100, 116, 139, 0.24)" },
          title: { display: true, text: "Units / day", color: "#94A3B8" },
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
      caption.textContent = "MAE unavailable in demo mode";
      if (demandChart) {
        demandChart.destroy();
        demandChart = null;
      }
      return;
    }
    const data = await fetchJSON(`/api/demand/${encodeURIComponent(sku)}`);
    caption.textContent = data.mae != null ? `Validation MAE ${data.mae.toFixed(2)} units/day` : "Validation MAE unavailable";
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
        setConnectionStatus(false, "Demo mode — backend unavailable on GitHub Pages");
        return;
      } catch (demoErr) {
      }
    }
    setConnectionStatus(false, `Offline — ${err.message}`);
  }
}

document.getElementById("refreshBtn").addEventListener("click", refresh);

refresh();
setInterval(refresh, POLL_MS);
