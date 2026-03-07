#!/bin/bash
# Setup script cho DeFi Trading Agent - chạy trong WSL
# Usage: ./setup_wsl.sh
# Dùng venv2: VENV=venv2 ./setup_wsl.sh

set -e

VENV_DIR="${VENV:-venv2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== DeFi Trading Agent - WSL Setup ==="

# 1. Tạo virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/4] Creating virtual environment ($VENV_DIR)..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/4] $VENV_DIR already exists"
fi

# 2. Activate và cài dependencies
echo "[2/4] Activating $VENV_DIR and installing dependencies..."
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

# 3. Tạo .env nếu chưa có
if [ ! -f ".env" ]; then
    echo "[3/4] Creating .env from template..."
    cp .env.example .env
    echo "    → Đã tạo .env. Vui lòng chỉnh sửa và điền API keys."
else
    echo "[3/4] .env already exists"
fi

# 4. Tạo thư mục data
mkdir -p data/logs
echo "[4/4] Created data/logs/"

echo ""
echo "=== Setup hoàn tất ==="
echo ""
echo "Chạy bot:"
echo "  source $VENV_DIR/bin/activate"
echo "  python main.py"
echo ""
echo "Web UI: http://localhost:8080 (tự động khi chạy main.py)"
echo ""
