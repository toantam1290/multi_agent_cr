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
    const style = data.trading_style ? ` (${data.trading_style})` : "";
    badge.textContent = (data.paper_trading ? "PAPER" : "LIVE") + style;
    badge.classList.toggle("live", !data.paper_trading);
  }
}

function renderOpenTrades(trades) {
  const tbody = document.getElementById("open-trades-body");
  if (!tbody) return;
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Chưa có position mở. Sẽ có khi signal được approve và execute.</td></tr>';
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
    tbody.innerHTML = '<tr><td colspan="6" class="empty">Chưa có signal. Cần pair pass rule filter + Claude PROCEED + confidence ≥ 80 (scalp).</td></tr>';
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
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Chưa có trade đóng. Sẽ có sau khi position hit SL/TP.</td></tr>';
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
    wrap.innerHTML = '<div class="empty">Chưa có logs. Agent sẽ ghi log khi scan/analyze.</div>';
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

function renderOpportunity(data) {
  const wrap = document.getElementById("opportunity-body");
  if (!wrap) return;
  if (!data) {
    wrap.innerHTML = '<div class="empty">No data</div>';
    return;
  }
  const cfg = data.config || {};
  const funnel = data.last_funnel;
  const funnelTime = data.last_funnel_time ? fmtTime(data.last_funnel_time) : "—";

  let html = `
    <div class="opportunity-config">
      <div class="opp-row"><span class="opp-label">Scan mode</span><span class="opp-value">${cfg.scan_mode || "—"}</span></div>
      <div class="opp-row"><span class="opp-label">Trading style</span><span class="opp-value">${cfg.trading_style || "—"}</span></div>
      <div class="opp-row"><span class="opp-label">Scan / Monitor</span><span class="opp-value">${cfg.scan_interval_min ?? "—"} min / ${cfg.position_monitor_interval_min ?? "—"} min</span></div>
      <div class="opp-row"><span class="opp-label">Dry run</span><span class="opp-value">${cfg.scan_dry_run ? "Yes" : "No"}</span></div>
      <div class="opp-row"><span class="opp-label">Volatility</span><span class="opp-value" title="24h price change % để vào opportunity list">${cfg.opportunity_volatility_pct ?? "—"}–${cfg.opportunity_volatility_max_pct ?? "—"}%</span></div>
      <div class="opp-row"><span class="opp-label">1h range min</span><span class="opp-value" title="Scalp: pair phải có 1-2h range ≥ này (coin active)">${cfg.scalp_1h_range_min_pct ?? "—"}%</span></div>
      <div class="opp-row"><span class="opp-label">Active hours UTC</span><span class="opp-value">${cfg.scalp_active_hours_utc ?? "—"}</span></div>
      <div class="opp-row"><span class="opp-label">Max pairs</span><span class="opp-value">${cfg.max_pairs_per_scan ?? "—"}</span></div>
      <div class="opp-row"><span class="opp-label">Core pairs</span><span class="opp-value">${(cfg.core_pairs || []).join(", ") || "—"}</span></div>
    </div>
  `;
  if (funnel) {
    const pairsList = funnel.pairs_scanned_list || [];
    const pairsStr = pairsList.length ? pairsList.map(p => p.replace("USDT", "")).join(", ") : "—";
    html += `
    <div class="opportunity-funnel">
      <h3>Last scan funnel (${funnelTime})</h3>
      <div class="funnel-grid">
        <div class="funnel-item"><span class="funnel-val">${funnel.opportunity_candidates ?? "—"}</span><span class="funnel-lbl">Candidates</span></div>
        <div class="funnel-item"><span class="funnel-val">${funnel.pairs_scanned ?? "—"}</span><span class="funnel-lbl">Scanned</span></div>
        <div class="funnel-item"><span class="funnel-val">${funnel.rule_based_passed ?? "—"}</span><span class="funnel-lbl">Rule passed</span></div>
        <div class="funnel-item"><span class="funnel-val">${funnel.claude_passed ?? "—"}</span><span class="funnel-lbl">Claude passed</span></div>
        <div class="funnel-item"><span class="funnel-val">${funnel.signals_generated ?? "—"}</span><span class="funnel-lbl">Signals</span></div>
      </div>
      ${pairsList.length ? `<p class="funnel-pairs" title="Pairs được scan trong cycle này">Pairs: <code>${pairsStr}</code></p>` : ""}
      ${funnel.fallback_used ? '<p class="funnel-warn">⚠ Fallback used (API failed)</p>' : ""}
    </div>
    `;
  } else {
    html += '<div class="empty">Chưa có funnel — chờ scan cycle chạy (mỗi 5 phút scalp).</div>';
  }
  wrap.innerHTML = html;
}

function renderDashboard(rows) {
  const tbody = document.getElementById("dashboard-body");
  if (!tbody) return;
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">Chưa có data ngày — cần có trades đóng để tính quality score.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const pnlClass = (r.net_pnl_usdt || 0) > 0 ? "positive" : (r.net_pnl_usdt || 0) < 0 ? "negative" : "";
    const actionClass = r.action === "SCALE_UP_SMALL" ? "action-scale" : r.action === "DEFENSIVE_MODE" ? "action-defensive" : r.action === "TIGHTEN_FILTER" ? "action-tighten" : "";
    return `
    <tr>
      <td>${r.date_utc || "—"}</td>
      <td>${r.signals_total ?? 0}</td>
      <td>${r.approved_signals ?? 0}</td>
      <td>${r.executed_trades ?? 0}</td>
      <td>${(r.win_rate_pct ?? 0).toFixed(1)}%</td>
      <td class="${pnlClass}">$${fmtNum(r.net_pnl_usdt)}</td>
      <td><span class="score-badge">${(r.quality_score ?? 0).toFixed(1)}</span></td>
      <td><span class="action-badge ${actionClass}">${r.action || "—"}</span></td>
    </tr>
    `;
  }).join("");
}

async function refresh() {
  try {
    const [stats, signalsRes, openRes, historyRes, logsRes, oppRes, dashRes] = await Promise.all([
      fetchJson("/stats"),
      fetchJson("/signals"),
      fetchJson("/trades/open"),
      fetchJson("/trades/history"),
      fetchJson("/logs"),
      fetchJson("/opportunity"),
      fetchJson("/daily-dashboard?days=7"),
    ]);

    renderStats(stats);
    renderSignals(signalsRes.signals || []);
    renderOpenTrades(openRes.trades || []);
    renderHistory(historyRes.trades || []);
    renderLogs(logsRes.logs || []);
    renderOpportunity(oppRes);
    renderDashboard(dashRes.rows || []);

    const lastEl = document.getElementById("last-update");
    if (lastEl) lastEl.textContent = "Updated " + new Date().toLocaleTimeString("vi-VN");
  } catch (e) {
    console.error("Refresh failed:", e);
  }
}

refresh();
setInterval(refresh, 5000);
