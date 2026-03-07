const API = "/api";

function fmtNum(n) {
  if (n == null || n === undefined) return "—";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPct(n) {
  if (n == null || n === undefined) return "—";
  const s = (Number(n) * 100).toFixed(1) + "%";
  return n >= 0 ? "+" + s : s;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("vi-VN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shortId(id) {
  return id ? id.slice(0, 8) : "—";
}

async function fetchJson(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

function setStat(id, value, positiveClass = "positive", negativeClass = "negative") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value;
  el.classList.remove(positiveClass, negativeClass);
  if (typeof value === "string" && (value.startsWith("+") || value.includes("$") && value.includes("+")))
    el.classList.add(positiveClass);
  else if (typeof value === "string" && value.startsWith("-"))
    el.classList.add(negativeClass);
}

function renderStats(data) {
  setStat("total-pnl", "$" + fmtNum(data.total_pnl_usdt));
  const totalEl = document.getElementById("total-pnl");
  if (totalEl) {
    const v = data.total_pnl_usdt || 0;
    totalEl.classList.toggle("positive", v > 0);
    totalEl.classList.toggle("negative", v < 0);
  }

  setStat("daily-pnl", "$" + fmtNum(data.daily_pnl_usdt));
  const dailyEl = document.getElementById("daily-pnl");
  if (dailyEl) {
    const v = data.daily_pnl_usdt || 0;
    dailyEl.classList.toggle("positive", v > 0);
    dailyEl.classList.toggle("negative", v < 0);
  }

  setStat("win-rate", (data.win_rate || 0).toFixed(1) + "%");
  setStat("open-positions", data.open_positions ?? 0);
  setStat("pending-signals", data.pending_signals ?? 0);

  const badge = document.getElementById("mode-badge");
  if (badge) {
    badge.textContent = data.paper_trading ? "PAPER" : "LIVE";
    badge.classList.toggle("live", !data.paper_trading);
  }
}

function renderOpenTrades(trades) {
  const tbody = document.getElementById("open-trades-body");
  if (!tbody) return;
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">No open positions</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map((t) => `
    <tr>
      <td><strong>${t.pair}</strong></td>
      <td><span class="dir-${t.direction.toLowerCase()}">${t.direction}</span></td>
      <td>$${fmtNum(t.entry_price)}</td>
      <td>$${fmtNum(t.stop_loss)} / $${fmtNum(t.take_profit)}</td>
      <td>$${fmtNum(t.position_size_usdt)}</td>
    </tr>
  `).join("");
}

function renderSignals(signals) {
  const tbody = document.getElementById("signals-body");
  if (!tbody) return;
  if (!signals.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No signals yet</td></tr>';
    return;
  }
  tbody.innerHTML = signals.slice(0, 20).map((s) => `
    <tr>
      <td>${fmtTime(s.created_at)}</td>
      <td><strong>${s.pair}</strong></td>
      <td><span class="dir-${s.direction.toLowerCase()}">${s.direction}</span></td>
      <td>${s.confidence ?? "—"}</td>
      <td>$${fmtNum(s.entry_price)}</td>
      <td><span class="status ${(s.status || "").toLowerCase()}">${s.status || "—"}</span></td>
    </tr>
  `).join("");
}

function renderHistory(trades) {
  const tbody = document.getElementById("history-body");
  if (!tbody) return;
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">No closed trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = trades.slice(0, 15).map((t) => {
    const pnl = t.pnl_usdt != null ? "$" + fmtNum(t.pnl_usdt) + " (" + fmtPct(t.pnl_pct) + ")" : "—";
    const pnlClass = t.pnl_usdt > 0 ? "positive" : t.pnl_usdt < 0 ? "negative" : "";
    return `
    <tr>
      <td><strong>${t.pair}</strong></td>
      <td><span class="dir-${t.direction.toLowerCase()}">${t.direction}</span></td>
      <td>$${fmtNum(t.entry_price)} → $${fmtNum(t.exit_price)}</td>
      <td class="${pnlClass}">${pnl}</td>
      <td><span class="status">${t.status || "—"}</span></td>
    </tr>
  `;
  }).join("");
}

function renderLogs(logs) {
  const wrap = document.getElementById("logs-body");
  if (!wrap) return;
  if (!logs.length) {
    wrap.innerHTML = '<div class="empty">No logs yet</div>';
    return;
  }
  wrap.innerHTML = logs.slice(0, 50).map((l) => `
    <div class="log-entry">
      <span class="log-time">${fmtTime(l.timestamp)}</span>
      <span class="log-agent">${l.agent || "—"}</span>
      <span class="log-level ${(l.level || "").toLowerCase()}">${l.level || ""}</span>
      <span class="log-msg">${escapeHtml(l.message || "")}</span>
    </div>
  `).join("");
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function refresh() {
  try {
    const [stats, signalsRes, openRes, historyRes, logsRes] = await Promise.all([
      fetchJson("/stats"),
      fetchJson("/signals"),
      fetchJson("/trades/open"),
      fetchJson("/trades/history"),
      fetchJson("/logs"),
    ]);

    renderStats(stats);
    renderSignals(signalsRes.signals || []);
    renderOpenTrades(openRes.trades || []);
    renderHistory(historyRes.trades || []);
    renderLogs(logsRes.logs || []);

    const lastEl = document.getElementById("last-update");
    if (lastEl) lastEl.textContent = "Updated " + new Date().toLocaleTimeString("vi-VN");
  } catch (e) {
    console.error("Refresh failed:", e);
  }
}

refresh();
setInterval(refresh, 5000);
