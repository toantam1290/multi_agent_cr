"""Utils module: market data fetchers."""
import threading

# Lock chia sẻ giữa ResearchAgent và SMCAgent để tránh TOCTOU trên pair cooldown
# Cả hai agents import lock này và dùng khi check + save signal
pair_scan_lock = threading.Lock()
