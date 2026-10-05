# Deploying on YunoHost

YunoHost is a Debian-based self-hosting platform. This app has no YunoHost package,
so installation is done manually via SSH — but everything works because YunoHost is
standard Debian under the hood.

## Prerequisites

- A YunoHost server with a domain configured (e.g. `tv.yourdomain.com`)
- SSH access to the server
- A free Groq API key from https://console.groq.com/keys
- ffmpeg installed (`sudo apt install ffmpeg`)

---

## Step 1 — Clone and start the app

```bash
ssh admin@yourdomain.com

# Install ffmpeg if not already present
sudo apt install -y ffmpeg

# Clone the repo somewhere outside the YunoHost web root
cd /opt
sudo git clone https://github.com/ukoquique-proves/cidadeTV-canal10.git tvcidade10
sudo chown -R $USER:$USER /opt/tvcidade10
cd /opt/tvcidade10

# First run: creates venv, installs deps, copies .env, starts the app
GROQ_API_KEY=gsk_... ./start.sh
```

The app starts on `http://127.0.0.1:8000`. Press Ctrl+C once you confirm it starts
cleanly — the systemd unit (Step 3) will keep it running permanently.

---

## Step 2 — Nginx reverse proxy

YunoHost manages nginx through its own config system. Add a custom snippet for the
subdomain. Create the file:

```bash
sudo nano /etc/nginx/conf.d/tv.yourdomain.com.d/tvcidade10.conf
```

Paste:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;

    # Required for WebSocket (captions)
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Allow long-lived WebSocket connections
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

> **Note:** The `Upgrade` and `Connection` headers are essential. Without them
> WebSocket connections silently fail and captions never arrive in the browser.

Test and reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

YunoHost handles HTTPS and Let's Encrypt certificates automatically for configured
subdomains — no extra steps needed.

---

## Step 3 — systemd service (auto-start on reboot + auto-restart on crash)

Create the service unit:

```bash
sudo nano /etc/systemd/system/tvcidade10.service
```

Paste (adjust `User` and paths if needed):

```ini
[Unit]
Description=TV Cidade 10 Live Subtitles
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=admin
WorkingDirectory=/opt/tvcidade10
ExecStart=/opt/tvcidade10/venv/bin/python main.py
Restart=always
RestartSec=10
# Environment is read from .env by the app itself — no EnvironmentFile needed.
# If you prefer to keep the key out of .env, uncomment the line below:
# Environment="GROQ_API_KEY=gsk_..."

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tvcidade10
sudo systemctl start tvcidade10

# Check it's running
sudo systemctl status tvcidade10
sudo journalctl -u tvcidade10 -f   # live logs
```

---

## Step 4 — Configure the subdomain in YunoHost

In the YunoHost admin panel (`https://yourdomain.com/yunohost/admin`):

1. Go to **Domains → tv.yourdomain.com → DNS configuration**
2. Confirm the subdomain points to your server's IP
3. Go to **Domains → tv.yourdomain.com → Certificate** → Install Let's Encrypt cert
   (if not already done automatically)

After this, `https://tv.yourdomain.com` should serve the player with a valid HTTPS cert.

---

## Step 5 — Check .env for public access

Edit `/opt/tvcidade10/.env` and make sure these are set (they are the defaults):

```dotenv
HOST=127.0.0.1    # nginx proxies from outside; the app only binds locally
PORT=8000         # must match proxy_pass in Step 2
```

The app does NOT need `HOST=0.0.0.0` when nginx is proxying — binding to localhost
is safer (the app is not directly exposed to the internet).

If the channel's playlist or segments are served from a different host than
`STREAM_URL`, add it to `PROXY_ALLOWED_HOSTS` (e.g. `.logicahost.com.br`); otherwise
the proxy answers 403 "Host not allowed" for those requests. After editing `.env`:
`sudo systemctl restart tvcidade10`.

---

## Verify it works

```bash
# Proxy serves the stream playlist
curl -s https://tv.yourdomain.com/proxy/playlist | head -5

# WebSocket upgrade succeeds (should return HTTP 101)
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
     -H "Sec-WebSocket-Version: 13" \
     https://tv.yourdomain.com/captions
```

Open `https://tv.yourdomain.com` in a browser — you should see the player.

---

## Updating the app

```bash
cd /opt/tvcidade10
git pull
sudo systemctl restart tvcidade10
```

If `requirements.groq.txt` changed, reinstall deps first:

```bash
source venv/bin/activate
pip install -r requirements.groq.txt
deactivate
sudo systemctl restart tvcidade10
```

---

## Monitoring

```bash
# Live logs
sudo journalctl -u tvcidade10 -f

# Check stream is online
./check_stream.sh

# App health
curl -s https://tv.yourdomain.com/health
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Page loads but no video | Stream offline or URL changed | Run `./check_stream.sh`; update `STREAM_URL` in `.env` |
| Captions never appear | WebSocket not upgrading | Check nginx config has `Upgrade`/`Connection` headers |
| 502 Bad Gateway | App not running | `sudo systemctl status tvcidade10` |
| 403 on `/proxy/segment` | HMAC signature mismatch (app restarted mid-session) | The page rebuilds the player by itself within ~1 s; if it does not, reload. A 403 "Host not allowed" instead means the host is missing from `PROXY_ALLOWED_HOSTS` |
| Spanish captions blank | Groq quota hit or key invalid | Check `sudo journalctl -u tvcidade10` for 429/401 errors |
| App doesn't restart after reboot | systemd unit not enabled | `sudo systemctl enable tvcidade10` |

---

## Security notes

- The app binds to `127.0.0.1` — not exposed directly to the internet.
- The HLS proxy uses HMAC-signed URLs to prevent open relay (SSRF).
- `PROXY_ALLOWED_HOSTS` in `.env` limits which upstream hosts the proxy will contact.
- Keep `GROQ_API_KEY` out of git — it lives only in `.env` which is gitignored.
- For extra security, create a dedicated system user instead of running as `admin`:
  ```bash
  sudo useradd -r -s /bin/false -d /opt/tvcidade10 tvcidade10svc
  sudo chown -R tvcidade10svc:tvcidade10svc /opt/tvcidade10
  # Then set User=tvcidade10svc in the systemd unit
  ```
