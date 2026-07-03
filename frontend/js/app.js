const isGitHubPages = window.location.hostname.endsWith("github.io");
const API = window.SMART_SHELF_API_BASE || "";
const POLL_MS = 10000;

let demandChart = null;
let selectedSku = null;
let demoModeActive = false;

let activityChart = null;
let activityPeriod = "1m";
let activityEvents = [];
let skuNames = {};

/* ---------- Data access (unchanged logic) ---------- */

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

/* ---------- System status ---------- */

function setConnectionStatus(live, text) {
  const el = document.getElementById("connectionStatus");
  el.classList.toggle("is-live", live);
  el.classList.toggle("is-down", !live);
  el.querySelector(".sys-text").textContent = text;

  const updatedChip = document.getElementById("lastUpdatedChip");
  if (updatedChip) {
    updatedChip.textContent = `Synced ${new Date().toLocaleTimeString()}`;
  }
}

/* ---------- Shelf status table ---------- */

function stockPercent(current, rop) {
  const max = Math.max(current, rop, 1);
  return Math.min(100, Math.round((current / max) * 100));
}

function ropPercent(current, rop) {
  const max = Math.max(current, rop, 1);
  return Math.min(100, Math.round((rop / max) * 100));
}

function slotStatus(s) {
  if (!s.needs_restock) return { label: "Stable", cls: "badge--ok", fill: "" };
  if (s.shortfall > 20) return { label: "Critical", cls: "badge--danger", fill: "is-danger" };
  return { label: "Low stock", cls: "badge--warn", fill: "is-warn" };
}

function renderSlots(slots) {
  const body = document.getElementById("slotGrid");
  if (!slots.length) {
    body.innerHTML = `
      <tr><td colspan="7">
        <div class="empty">
          <p class="empty__title">No slots configured</p>
          <p class="empty__body">Add slot-to-SKU mappings in <code>config.py</code>, then restart the API.</p>
        </div>
      </td></tr>`;
    return;
  }

  body.innerHTML = slots
    .map((s) => {
      const st = slotStatus(s);
      const fillPct = stockPercent(s.current_stock, s.reorder_point);
      const tickPct = ropPercent(s.current_stock, s.reorder_point);
      return `
        <tr>
          <td class="mono cell-muted">${String(s.slot_id).padStart(2, "0")}</td>
          <td class="cell-strong">${s.name}</td>
          <td class="mono cell-muted">${s.sku}</td>
          <td class="num cell-strong">${s.current_stock}</td>
          <td class="num cell-muted">${s.reorder_point}</td>
          <td>
            <span class="rail-meter" role="img"
              aria-label="${s.current_stock} units on hand, reorder point ${s.reorder_point}">
              <span class="rail-meter__fill ${st.fill}" style="width:${fillPct}%"></span>
              <span class="rail-meter__tick" style="left:${tickPct}%"></span>
            </span>
          </td>
          <td><span class="badge ${st.cls}">${st.label}</span></td>
        </tr>
      `;
    })
    .join("");
}

/* ---------- Event log ---------- */

function renderEvents(events) {
  const body = document.getElementById("eventBody");
  if (!events.length) {
    body.innerHTML = `
      <tr><td colspan="4">
        <div class="empty">
          <p class="empty__title">No events yet</p>
          <p class="empty__body">Start <code>serial_listener.py</code> and trigger the shelf sensors to record activity.</p>
        </div>
      </td></tr>`;
    return;
  }

  body.innerHTML = events
    .map(
      (e) => `
      <tr>
        <td class="num cell-muted">${e.id}</td>
        <td class="num">${e.slot_id}</td>
        <td class="mono">${e.sku}</td>
        <td class="mono cell-muted">${e.time}</td>
      </tr>
    `
    )
    .join("");
}

/* ---------- Removal activity ---------- */

const PERIOD_SECONDS = {
  "1d": 86400,
  "1w": 7 * 86400,
  "1m": 30 * 86400,
  "3m": 90 * 86400,
  all: null,
};

function bucketize(events, windowSec) {
  if (!events.length) return { labels: [], counts: [] };

  const nowSec = Date.now() / 1000;
  const startSec =
    windowSec != null
      ? nowSec - windowSec
      : Math.min(...events.map((e) => e.timestamp));
  const spanSec = nowSec - startSec;

  const hourly = spanSec <= 2 * 86400;
  const bucketSec = hourly ? 3600 : 86400;
  const bucketCount = Math.max(1, Math.ceil(spanSec / bucketSec));

  const counts = new Array(bucketCount).fill(0);
  events.forEach((e) => {
    const idx = Math.floor((e.timestamp - startSec) / bucketSec);
    if (idx >= 0 && idx < bucketCount) counts[idx] += 1;
  });

  const labels = counts.map((_, i) => {
    const d = new Date((startSec + i * bucketSec) * 1000);
    return hourly
      ? `${String(d.getHours()).padStart(2, "0")}:00`
      : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  });

  return { labels, counts };
}

function renderActivityChart(labels, counts) {
  const ctx = document.getElementById("activityChart").getContext("2d");
  const data = {
    labels,
    datasets: [
      {
        data: counts,
        borderColor: "#226b4d",
        backgroundColor: "rgba(34, 107, 77, 0.08)",
        borderWidth: 2,
        pointRadius: 0,
        pointHitRadius: 8,
        tension: 0.35,
        fill: true,
      },
    ],
  };

  if (activityChart) {
    activityChart.data = data;
    activityChart.update("none");
    return;
  }

  activityChart = new Chart(ctx, {
    type: "line",
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#1c1e21",
          titleColor: "#f2f3f5",
          bodyColor: "#c6c9cd",
          titleFont: { family: "'Geist Mono', monospace", size: 11 },
          bodyFont: { family: "'Geist Mono', monospace", size: 11 },
          padding: 8,
          cornerRadius: 6,
          displayColors: false,
          callbacks: {
            label: (item) => `${item.parsed.y} removal${item.parsed.y === 1 ? "" : "s"}`,
          },
        },
      },
      scales: {
        x: { display: false },
        y: {
          display: false,
          beginAtZero: true,
          grace: "15%",
        },
      },
    },
  });
}

function formatDelta(current, previous) {
  if (previous === 0) return { text: "—", cls: "" };
  const pct = ((current - previous) / previous) * 100;
  const rounded = Math.round(pct);
  if (rounded === 0) return { text: "0%", cls: "" };
  return {
    text: `${rounded > 0 ? "+" : ""}${rounded}%`,
    cls: rounded > 0 ? "is-up" : "is-down",
  };
}

function renderActivity() {
  const totalEl = document.getElementById("activityTotal");
  const changeEl = document.getElementById("activityChange");
  const listEl = document.getElementById("activityList");

  const windowSec = PERIOD_SECONDS[activityPeriod];
  const nowSec = Date.now() / 1000;

  const inWindow =
    windowSec == null
      ? activityEvents
      : activityEvents.filter((e) => e.timestamp >= nowSec - windowSec);
  const inPrevWindow =
    windowSec == null
      ? []
      : activityEvents.filter(
          (e) => e.timestamp >= nowSec - 2 * windowSec && e.timestamp < nowSec - windowSec
        );

  totalEl.textContent = inWindow.length;

  if (windowSec != null && inPrevWindow.length > 0) {
    const delta = formatDelta(inWindow.length, inPrevWindow.length);
    changeEl.textContent = delta.text;
    changeEl.className = `badge ${delta.cls === "is-down" ? "badge--danger" : "badge--ok"}`;
    changeEl.hidden = false;
  } else {
    changeEl.hidden = true;
  }

  const { labels, counts } = bucketize(inWindow, windowSec);
  renderActivityChart(labels, counts);

  const skus = [...new Set(activityEvents.map((e) => e.sku))].sort();
  if (!skus.length) {
    listEl.innerHTML =
      '<li class="activity__empty">No removals recorded yet. Trigger the shelf sensors to see activity.</li>';
    return;
  }

  listEl.innerHTML = skus
    .map((sku) => {
      const cur = inWindow.filter((e) => e.sku === sku).length;
      const prev = inPrevWindow.filter((e) => e.sku === sku).length;
      const delta = windowSec == null ? { text: "", cls: "" } : formatDelta(cur, prev);
      return `
        <li class="activity__row">
          <span class="sku-chip" aria-hidden="true">${sku.slice(-1)}</span>
          <span class="activity__name">${skuNames[sku] || sku}</span>
          <span class="activity__count">${cur}</span>
          <span class="activity__delta ${delta.cls}">${delta.text}</span>
        </li>
      `;
    })
    .join("");
}

document.querySelectorAll("#activityPeriods button").forEach((btn) => {
  btn.addEventListener("click", () => {
    activityPeriod = btn.dataset.period;
    document.querySelectorAll("#activityPeriods button").forEach((b) => {
      const active = b === btn;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-pressed", active);
    });
    renderActivity();
  });
});

/* ---------- Restock plan ---------- */

function renderRecommendations(slots, config) {
  const container = document.getElementById("recommendations");
  const meta = document.getElementById("configMeta");
  const initial = config.initial_stock_per_slot?.["1"] ?? "N/A";
  meta.textContent = `Lead time ${config.lead_time_days} days · Z-score ${config.z_score} · Initial stock per slot ${initial}`;

  if (!slots.length) {
    container.innerHTML = `
      <div class="empty">
        <p class="empty__title">Nothing to plan yet</p>
        <p class="empty__body">Restock guidance appears here once slots report stock levels.</p>
      </div>`;
    return;
  }

  container.innerHTML = slots
    .map((s) => {
      const st = slotStatus(s);
      const open = s.needs_restock ? "plan-item--open" : "";
      return `
        <div class="plan-item ${open}" data-slot="${s.slot_id}">
          <button type="button" class="plan-item__toggle" aria-expanded="${s.needs_restock}">
            <span class="plan-item__title">
              <span class="plan-item__chev" aria-hidden="true">▶</span>
              Slot ${s.slot_id} — ${s.name} <span class="cell-muted mono">${s.sku}</span>
            </span>
            <span class="badge ${st.cls}">${s.needs_restock ? "Restock needed" : "Stock OK"}</span>
          </button>
          <div class="plan-item__body">
            <div>
              <h4>Inventory</h4>
              <ul class="stat-list">
                <li><span class="k">Units on hand</span><span class="v">${s.current_stock}</span></li>
                <li><span class="k">Removals, last 30 days</span><span class="v">${s.removals_in_window}</span></li>
                <li><span class="k">Predicted demand / day</span><span class="v">${s.predicted_daily_demand}</span></li>
                ${
                  s.needs_restock
                    ? `<li><span class="k">Shortfall</span><span class="v is-danger">${s.shortfall.toFixed(1)} units</span></li>`
                    : ""
                }
              </ul>
            </div>
            <div>
              <h4>Reorder point calculation</h4>
              <pre class="math">${s.reorder_math}</pre>
            </div>
          </div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".plan-item__toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const item = btn.closest(".plan-item");
      const open = item.classList.toggle("plan-item--open");
      btn.setAttribute("aria-expanded", open);
    });
  });
}

/* ---------- KPI summary ---------- */

function renderSummary(health, slots) {
  const restockCount = slots.filter((s) => s.needs_restock).length;
  const modelReady = health.model_loaded;

  document.getElementById("statSlots").textContent = slots.length;
  document.getElementById("hintSlots").textContent = "Mapped in config.py";

  const restockEl = document.getElementById("statRestock");
  restockEl.textContent = restockCount;
  restockEl.classList.toggle("is-warn", restockCount > 0);
  restockEl.classList.toggle("is-ok", restockCount === 0);
  const hintRestock = document.getElementById("hintRestock");
  hintRestock.textContent =
    restockCount > 0 ? "Action needed — see restock plan" : "All slots above reorder point";
  hintRestock.classList.toggle("is-warn", restockCount > 0);

  document.getElementById("statEvents").textContent = health.event_count;
  document.getElementById("hintEvents").textContent = "Sensor removals logged to date";

  const modelEl = document.getElementById("statModel");
  modelEl.textContent = modelReady ? "Ready" : "Missing";
  modelEl.classList.toggle("is-ok", modelReady);
  modelEl.classList.toggle("is-danger", !modelReady);
  document.getElementById("hintModel").textContent = modelReady
    ? "Gradient boosting, trained"
    : "Run python train_model.py";

  const modelChip = document.getElementById("modelReadyChip");
  if (modelChip) {
    modelChip.textContent = `Model ${modelReady ? "ready" : "missing"}`;
  }
}

/* ---------- Demand chart ---------- */

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
        borderColor: "#8b8f96",
        backgroundColor: "rgba(139, 143, 150, 0.08)",
        borderWidth: 1.5,
        pointRadius: 0,
        pointHitRadius: 8,
        tension: 0.3,
        spanGaps: false,
        fill: true,
      },
      {
        label: "Predicted",
        data: allDates.map((d) => (d in predMap ? predMap[d] : null)),
        borderColor: "#226b4d",
        backgroundColor: "rgba(34, 107, 77, 0.1)",
        borderWidth: 2,
        borderDash: [5, 4],
        pointRadius: 2.5,
        pointBackgroundColor: "#226b4d",
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

  const mono = "'Geist Mono', monospace";

  demandChart = new Chart(ctx, {
    type: "line",
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          align: "end",
          labels: {
            color: "#5b5f66",
            font: { family: mono, size: 10 },
            boxWidth: 16,
            boxHeight: 2,
          },
        },
        tooltip: {
          backgroundColor: "#1c1e21",
          borderColor: "#1c1e21",
          borderWidth: 1,
          titleColor: "#f2f3f5",
          bodyColor: "#c6c9cd",
          titleFont: { family: mono, size: 11 },
          bodyFont: { family: mono, size: 11 },
          padding: 10,
          cornerRadius: 6,
          displayColors: true,
        },
      },
      scales: {
        x: {
          ticks: {
            color: "#8b8f96",
            maxTicksLimit: 8,
            font: { family: mono, size: 10 },
          },
          grid: { display: false },
          border: { color: "#e7e5e0" },
        },
        y: {
          ticks: {
            color: "#8b8f96",
            font: { family: mono, size: 10 },
          },
          grid: { color: "#f0efec" },
          border: { display: false },
          title: {
            display: true,
            text: "Units / day",
            color: "#8b8f96",
            font: { family: mono, size: 10 },
          },
        },
      },
    },
  });
}

async function loadDemand(sku) {
  const caption = document.getElementById("chartMae");
  if (!sku) {
    caption.textContent = "Train the model to see demand forecasts.";
    return;
  }

  try {
    if (demoModeActive) {
      caption.textContent = "Validation MAE unavailable in demo mode";
      if (demandChart) {
        demandChart.destroy();
        demandChart = null;
      }
      return;
    }
    const data = await fetchJSON(`/api/demand/${encodeURIComponent(sku)}`);
    caption.textContent =
      data.mae != null
        ? `Validation MAE ${data.mae.toFixed(2)} units/day`
        : "Validation MAE unavailable";
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
    select.innerHTML = '<option value="">No products</option>';
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

/* ---------- Refresh loop (unchanged logic) ---------- */

async function refresh() {
  try {
    if (demoModeActive) {
      return;
    }
    const [health, config, slotsData, eventsData, skuData] = await Promise.all([
      fetchJSON("/api/health"),
      fetchJSON("/api/config"),
      fetchJSON("/api/slots"),
      fetchJSON("/api/events?limit=500"),
      fetchJSON("/api/skus"),
    ]);

    const slots = slotsData.slots;
    skuNames = Object.fromEntries(slots.map((s) => [s.sku, s.name]));
    activityEvents = eventsData.events;
    renderSummary(health, slots);
    renderSlots(slots);
    renderEvents(eventsData.events.slice(0, 100));
    renderRecommendations(slots, config);
    renderActivity();

    if (!selectedSku && skuData.skus.length) {
      await populateSkuSelect(skuData.skus, skuData.metrics);
    }

    setConnectionStatus(true, "Live");
  } catch (err) {
    if (isGitHubPages && !demoModeActive) {
      try {
        const demo = await loadDemoData();
        skuNames = Object.fromEntries(demo.slots.map((s) => [s.sku, s.name]));
        activityEvents = demo.events;
        renderSummary(demo.health, demo.slots);
        renderSlots(demo.slots);
        renderEvents(demo.events);
        renderRecommendations(demo.slots, demo.config);
        renderActivity();
        if (demo.skus.length) {
          await populateSkuSelect(demo.skus, demo.metrics || {});
        }
        demoModeActive = true;
        setConnectionStatus(false, "Demo data — API offline");
        return;
      } catch (demoErr) {
        // fall through to the generic offline state
      }
    }
    setConnectionStatus(false, `Offline — ${err.message}`);
  }
}

const refreshBtn = document.getElementById("refreshBtn");
refreshBtn.addEventListener("click", async () => {
  refreshBtn.classList.add("is-loading");
  refreshBtn.textContent = "Refreshing…";
  await refresh();
  refreshBtn.classList.remove("is-loading");
  refreshBtn.textContent = "Refresh data";
});

/* ---------- Navigation ---------- */

const sidebar = document.querySelector(".sidebar");
const navToggle = document.getElementById("navToggle");
navToggle.addEventListener("click", () => {
  const open = sidebar.classList.toggle("is-open");
  navToggle.setAttribute("aria-expanded", open);
});

const navLinks = [...document.querySelectorAll(".nav-link[href^='#']")];
navLinks.forEach((link) => {
  link.addEventListener("click", () => {
    sidebar.classList.remove("is-open");
    navToggle.setAttribute("aria-expanded", "false");
  });
});

const sections = navLinks
  .map((link) => document.querySelector(link.getAttribute("href")))
  .filter(Boolean);

const spy = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      navLinks.forEach((link) =>
        link.classList.toggle("is-active", link.getAttribute("href") === `#${entry.target.id}`)
      );
    });
  },
  { rootMargin: "-30% 0px -60% 0px" }
);
sections.forEach((section) => spy.observe(section));

/* ---------- Scroll reveal ---------- */

const revealTargets = [
  ...document.querySelectorAll(".kpi, .main > .card, .split > .card, .duo > .card, .foot"),
];

if ("IntersectionObserver" in window) {
  const revealObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        revealObserver.unobserve(entry.target);
      });
    },
    { threshold: 0.15, rootMargin: "0px 0px -40px 0px" }
  );

  revealTargets.forEach((el, i) => {
    el.classList.add("reveal");
    if (el.classList.contains("kpi")) {
      el.style.setProperty("--reveal-delay", `${(i % 4) * 70}ms`);
    }
    revealObserver.observe(el);
  });
}

refresh();
setInterval(refresh, POLL_MS);
