# Deploy Trading Bot

## Yêu cầu
- Docker + Docker Compose
- Server với 1GB+ RAM (Oracle Cloud Free Tier recommended)

## Quick Start

### 1. Clone repo
```bash
git clone https://github.com/toantam1290/multi_agent_cr.git
cd multi_agent_cr
```

### 2. Tạo .env
```bash
cp .env.example .env
nano .env   # Điền API keys
```

Bắt buộc điền:
- `ANTHROPIC_API_KEY` — lấy từ https://console.anthropic.com
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — hoặc set `SKIP_TELEGRAM=true`

### 3. Chạy
```bash
docker compose up -d
```

Bot sẽ start và Web UI ở `http://localhost:8080`.

### 4. Xem log
```bash
docker compose logs -f bot
```

### 5. Dừng / Restart
```bash
docker compose down        # Dừng
docker compose restart bot # Restart
docker compose up -d --build  # Rebuild sau khi update code
```

---

## Truy cập Web UI từ xa

### Option A: SSH Tunnel (đơn giản, bảo mật)
Từ máy local:
```bash
ssh -L 8080:localhost:8080 ubuntu@<server-ip>
```
Mở browser: `http://localhost:8080`

### Option B: Cloudflare Tunnel (free HTTPS, không cần mở port)

#### Bước 1: Tạo tunnel
1. Vào https://one.dash.cloudflare.com → Zero Trust → Networks → Tunnels
2. Create tunnel → đặt tên (vd: `trading-bot`)
3. Copy **tunnel token** (dạng `eyJh...`)

#### Bước 2: Thêm vào .env
```
CLOUDFLARE_TUNNEL_TOKEN=eyJh...paste_token_here...
```

#### Bước 3: Bật cloudflared trong docker-compose.yml
Bỏ comment phần `cloudflared` service:
```yaml
  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: cloudflared
    restart: unless-stopped
    command: tunnel run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
    depends_on:
      - bot
```

#### Bước 4: Config route trong Cloudflare Dashboard
- Public hostname: chọn domain/subdomain (vd: `bot.yourdomain.com`)
- Service: `http://bot:8080`

#### Bước 5: Restart
```bash
docker compose up -d
```

Truy cập: `https://bot.yourdomain.com`

### Option C: Cloudflare Quick Tunnel (không cần domain, tạm thời)
```bash
# Trên server, chạy trực tiếp:
docker run --rm --network host cloudflare/cloudflared:latest tunnel --url http://localhost:8080
```
Sẽ cho URL dạng `https://xxx-yyy.trycloudflare.com` — URL thay đổi mỗi lần restart.

---

## Deploy trên Oracle Cloud Free Tier

### 1. Tạo VM
1. Vào https://cloud.oracle.com → Create VM
2. Image: **Ubuntu 22.04** (hoặc Oracle Linux)
3. Shape: **VM.Standard.A1.Flex** (ARM) — chọn 1 OCPU + 6GB RAM là đủ
4. Download SSH key

### 2. SSH vào VM
```bash
ssh -i <key.pem> ubuntu@<public-ip>
```

### 3. Cài Docker
```bash
# Ubuntu
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
# Logout rồi login lại
```

### 4. Clone + Deploy
```bash
git clone https://github.com/toantam1290/multi_agent_cr.git
cd multi_agent_cr
cp .env.example .env
nano .env   # Điền API keys
docker compose up -d
```

### 5. Mở port (nếu không dùng Cloudflare Tunnel)
**Oracle Cloud Console:**
- Networking → VCN → Subnet → Security List
- Add Ingress Rule: Source `0.0.0.0/0`, TCP, Port `8080`

**Firewall VM:**
```bash
sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT
sudo apt install -y iptables-persistent   # Auto save rules
```

---

## Update code
```bash
cd multi_agent_cr
git pull
docker compose up -d --build
```

## Troubleshooting

| Vấn đề | Fix |
|---------|-----|
| Bot crash loop | `docker compose logs bot` xem lỗi |
| DB locked | Chỉ 1 container chạy bot, không chạy song song |
| Port 8080 không truy cập | Check firewall + Oracle security list |
| ARM build chậm | Lần đầu build ~5 phút (compile numpy/pandas), sau đó cache |
| Binance 429 | Rate limiter tự xử lý, check log |
