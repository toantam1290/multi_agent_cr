"""
web/app.py - Web UI cho Trading Agent
Chạy độc lập: python -m web.app
Hoặc tích hợp vào main.py
"""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import cfg, DB_PATH
from database import Database

app = FastAPI(title="Trading Agent Dashboard", docs_url=None, redoc_url=None)
db = Database(DB_PATH)

# Mount static
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/stats")
async def api_stats():
    stats = db.get_stats()
    daily_pnl = db.get_daily_pnl()
    open_count = len(db.get_open_trades())
    pending_count = len(db.get_pending_signals())
    return {
        **stats,
        "daily_pnl_usdt": daily_pnl,
        "open_positions": open_count,
        "pending_signals": pending_count,
        "paper_trading": cfg.trading.paper_trading,
        "paper_balance_usdt": cfg.trading.paper_balance_usdt,
        "trading_style": cfg.scan.trading_style,
    }


@app.get("/api/signals")
async def api_signals(limit: int = 50):
    signals = db.get_recent_signals(limit)
    return {"signals": signals}


@app.get("/api/trades/open")
async def api_open_trades():
    trades = db.get_open_trades()
    return {"trades": trades}


@app.get("/api/trades/history")
async def api_trade_history(limit: int = 50):
    trades = db.get_recent_trades(limit)
    return {"trades": trades}


@app.get("/api/logs")
async def api_logs(limit: int = 100):
    logs = db.get_recent_logs(limit)
    return {"logs": logs}


@app.get("/api/opportunity")
async def api_opportunity():
    """Scan config + last funnel metrics từ agent_logs."""
    sc = cfg.scan
    config = {
        "scan_mode": sc.scan_mode,
        "trading_style": sc.trading_style,
        "scan_interval_min": sc.scan_interval_min,
        "position_monitor_interval_min": sc.position_monitor_interval_min,
        "scan_dry_run": sc.scan_dry_run,
        "opportunity_volatility_pct": sc.opportunity_volatility_pct,
        "opportunity_volatility_max_pct": sc.opportunity_volatility_max_pct,
        "min_quote_volume_usd": sc.min_quote_volume_usd,
        "max_pairs_per_scan": sc.max_pairs_per_scan,
        "core_pairs": sc.core_pairs,
        "scalp_1h_range_min_pct": sc.scalp_1h_range_min_pct,
        "scalp_active_hours_utc": sc.scalp_active_hours_utc or "(24/7)",
    }
    # Lấy funnel gần nhất từ logs
    logs = db.get_recent_logs(limit=100)
    last_funnel = None
    last_funnel_time = None
    for l in logs:
        if l.get("message") == "Scan cycle funnel" and l.get("data"):
            try:
                data = json.loads(l["data"]) if isinstance(l["data"], str) else l["data"]
                last_funnel = data
                last_funnel_time = l.get("timestamp")
                break
            except (json.JSONDecodeError, TypeError):
                pass
    return {"config": config, "last_funnel": last_funnel, "last_funnel_time": last_funnel_time}


@app.get("/api/daily-dashboard")
async def api_daily_dashboard(days: int = 7):
    """Daily metrics + quality score (spec 006)."""
    from utils.daily_metrics_report import get_dashboard_data
    data = get_dashboard_data(days=days)
    return {"days": days, "rows": data}


def run_server(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server(port=8080)
