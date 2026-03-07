"""
web/app.py - Web UI cho Trading Agent
Chạy độc lập: python -m web.app
Hoặc tích hợp vào main.py
"""
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


def run_server(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server(port=8080)
