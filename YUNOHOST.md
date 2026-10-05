# Deploying on YunoHost

This follows the same pattern as the `kilombo-wp` project: **everything that touches
the server goes through YunoHost itself** (domain, certificate, app, permission,
backup) instead of hand-edited nginx files.

Why this matters: YunoHost's login layer (SSOwat) only lets a request through when it
matches a permission that belongs to a registered app. A hand-written nginx snippet
has no permission, so visitors are redirected to the YunoHost login. tv10 therefore
has to sit behind a real YunoHost app, and be opened to the public with
`yunohost user permission add <app>.main visitors` — exactly what
`wp-setup.sh publish` does for WordPress.

```
visitor ──https──▶ nginx (YunoHost, Let's Encrypt) ──▶ Reverse-Proxy app ──▶ 127.0.0.1:8000
                                                                              tvcidade10.service (this repo)
```

> **Status of this guide.** The domain / certificate / backup / permission steps are
> the ones already used successfully for `new.kilombo.top`. The **Reverse-Proxy app**
> (Step 5) has *not* been tried on this server yet: confirm it exists in your catalog
> and read each prompt before answering. Steps marked ⚠ depend on it.

---

## Working rules (from kilombo-wp `AGENT.md`)

1. One step at a time. Read the output before the next step.
2. Back up before every risky change and write the archive name in `CHANGELOG.md`.
3. Never paste passwords or API keys into files, commands or commit messages.
   The Groq key is typed into `.env` on the server with an editor — never on a command line.
4. If a command fails, **stop and report the exact error**. Do not retry variations
   (fail2ban has already banned this operator's IP once).
5. Do not touch the other apps on the server (old SPIP, `/vpnadmin`, `/neutrinet`,
   WordPress, firewall, VPN).

---

## Step 0 — Choose the domain and point DNS at the server

**`tv10.cidade` cannot work.** As far as I know `.cidade` is not a real top-level
domain, so there is no public DNS for it and Let's Encrypt can never issue a
certificate for it. Use **`tv10.cidade.top`** — this needs you to own (or register)
`cidade.top`. This guide uses that name; replace it everywhere if you pick another.
(`tv.kilombo.top` is deliberately not used.)

Create a DNS record at wherever `cidade.top` is managed:

| Type | Name | Value |
|---|---|---|
| A | `tv10` | the server's public IPv4 |
| AAAA | `tv10` | the server's IPv6, only if the server has one |

A wildcard `*` record for `cidade.top` also works. After DNS propagates, on your
own machine:

```bash
getent hosts tv10.cidade.top      # must print the server's IP
```

---

## Step 1 — Preflight (on the server)

SSH in the same way as for kilombo-wp (key authentication, non-default port).

```bash
free -m | awk '/^Mem:/{print "RAM available MB:", $7}'
df -m / | awk 'NR==2{print "disk free MB:", $4}'
python3 --version                                 # needs 3.10+
sudo apt install -y ffmpeg git python3-venv       # python3-venv: Debian needs it for venv
getent hosts tv10.cidade.top                      # DNS resolves
sudo yunohost app list                            # note what is installed; do not touch it
sudo yunohost app search reverse                  # ⚠ find the Reverse Proxy app id
```

The Groq-only install is light (about 50 MB of dependencies, no local models).

---

## Step 2 — Add the domain and the certificate

```bash
sudo yunohost domain add tv10.cidade.top
sudo yunohost diagnosis run dnsrecords web
sudo yunohost diagnosis show --issues             # fix anything about tv10.cidade.top first
sudo yunohost domain cert install tv10.cidade.top # Let's Encrypt
```

---

## Step 3 — Install tv10 itself (as its own system user)

```bash
sudo useradd --system --home-dir /opt/tvcidade10 --shell /usr/sbin/nologin tv10svc
sudo git clone https://github.com/ukoquique-proves/cidadeTV-canal10.git /opt/tvcidade10
sudo chown -R tv10svc:tv10svc /opt/tvcidade10

# Create .env and put the key in with an editor (not on the command line)
sudo -u tv10svc cp /opt/tvcidade10/.env.example /opt/tvcidade10/.env
sudo -u tv10svc chmod 600 /opt/tvcidade10/.env
sudo -u tv10svc nano /opt/tvcidade10/.env
```

In the editor: uncomment `GROQ_API_KEY=` and paste the key
(free key: https://console.groq.com/keys). Check `STREAM_URL`; leave `HOST=127.0.0.1`
and `PORT=8000`. A Groq-only install has no local models, so **the key is required**.

First run (creates the venv, installs dependencies, starts the app):

```bash
sudo -u tv10svc /opt/tvcidade10/start.sh
```

In a second SSH session:

```bash
curl -s http://127.0.0.1:8000/health             # {"status":"ok","ws_clients":0}
```

Then press Ctrl+C in the first session — systemd will run it from now on.

---

## Step 4 — systemd service

```bash
sudo nano /etc/systemd/system/tvcidade10.service
```

```ini
[Unit]
Description=TV Cidade 10 Live Subtitles
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tv10svc
Group=tv10svc
WorkingDirectory=/opt/tvcidade10
ExecStart=/opt/tvcidade10/venv/bin/python main.py
Restart=always
RestartSec=10
# The app reads /opt/tvcidade10/.env itself — no EnvironmentFile needed.

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tvcidade10
sudo systemctl status tvcidade10
curl -s http://127.0.0.1:8000/health
```

---

## Step 5 — ⚠ Register the app in YunoHost (private at first)

> **Three things have not been verified on this server yet. Read before you type.**
>
> 1. **Is the Reverse-Proxy app in your catalog?**
>    `sudo yunohost app search reverse` (from Step 1) must return it. If it does not
>    appear, this step cannot proceed — stop and find the correct app id or an
>    alternative before continuing.
>
> 2. **What does the app ask during install?**
>    The exact prompts are unknown. Read each one before answering. In particular:
>    if it asks who may access the app, choose the **private / admins-only** option —
>    do not make it public yet (Step 7 does that deliberately, after a backup).
>    After install, confirm with `yunohost user permission list` that visitors are
>    **not** listed — some apps add them automatically.
>
> 3. **Does its nginx file include the WebSocket lines?**
>    Without `Upgrade`, `Connection "upgrade"`, and the timeout headers, captions
>    fail silently — the page loads but nothing ever arrives. The check below tells
>    you exactly what is missing. A later `yunohost app upgrade` may overwrite any
>    manual edits to this file, so re-check it after every upgrade of the proxy app.

```bash
sudo yunohost app install <reverse-proxy-app-id>   # id from Step 1; interactive
```

Answer the prompts: domain `tv10.cidade.top`, path `/`, destination = the local
port 8000 (read the prompt for the exact format it wants). If it asks who may access
it, choose the private option, as was done for WordPress (`access=admins`).

Then note the app id YunoHost gave it and look at what was created:

```bash
export APP_ID=<app id from: sudo yunohost app list>
sudo yunohost user permission list | grep -A6 "$APP_ID.main"   # should NOT include visitors yet
sudo cat /etc/nginx/conf.d/tv10.cidade.top.d/$APP_ID.conf
```

**WebSocket check (captions travel over a WebSocket).** Inside the `location /` block
of that file there must be all of:

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
```

If any are missing, add them (`sudo nano` the file). If `nginx -t` then complains that a
directive is duplicate, remove the duplicate line. Then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Keep a copy of the final block in your notes: YunoHost may rewrite this file when the
proxy app is upgraded, so re-check it after any `yunohost app upgrade`.

Private check (the SSO login redirect is expected here):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://tv10.cidade.top/    # expect 302
```

---

## Step 6 — Backup, then record it

```bash
sudo yunohost backup create --name tv10-$(date +%Y%m%d-%H%M) --apps $APP_ID
ls -lh /home/yunohost.backup/archives/ | tail -3
```

Write the archive name in `CHANGELOG.md`. This backs up the YunoHost side (the proxy
app and its config). The code is in git, and the Groq key can be re-issued, so
nothing else needs saving.

---

## Step 7 — Open it to the public

```bash
sudo yunohost user permission add $APP_ID.main visitors
```

---

## Verify (from your own computer, not the server)

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://tv10.cidade.top/     # expect 200
curl -s https://tv10.cidade.top/health                                # {"status":"ok",...}
curl -s https://tv10.cidade.top/proxy/playlist | head -5              # #EXTM3U ...

# WebSocket upgrade must answer HTTP/1.1 101
curl -i -N -m 3 -H "Connection: Upgrade" -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
     https://tv10.cidade.top/captions | head -3
```

Then open `https://tv10.cidade.top` in a private browser window: the video should play
and, once the channel is speaking, Portuguese captions appear (tick "Mostrar ES" for Spanish).

---

## Updating the app

```bash
cd /opt/tvcidade10
sudo -u tv10svc git pull
sudo systemctl restart tvcidade10
```

If `requirements.groq.txt` changed, install before restarting:

```bash
sudo -u tv10svc /opt/tvcidade10/venv/bin/pip install -r /opt/tvcidade10/requirements.groq.txt
```

Open browser tabs recover by themselves after a restart (the page rebuilds the player
when its signed stream URLs stop working).

---

## Monitoring

```bash
sudo journalctl -u tvcidade10 -f                   # live logs (look for 429/401 from Groq)
cd /opt/tvcidade10 && sudo -u tv10svc ./check_stream.sh   # is the TV stream itself up?
curl -s https://tv10.cidade.top/health
```

Run `check_stream.sh` as `tv10svc`: `.env` is private to that user, and otherwise the
script silently falls back to the default stream URL.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Public visitors land on the YunoHost login (302) | `visitors` permission not added | Step 7; check with `sudo yunohost user permission list` |
| Certificate step fails | DNS not pointing at the server yet | `getent hosts tv10.cidade.top`; `sudo yunohost diagnosis run dnsrecords web` |
| 502 Bad Gateway | tv10 service is down, or wrong port in the proxy app | `sudo systemctl status tvcidade10`; destination must be port 8000 |
| Page loads, captions never arrive | WebSocket headers missing in the nginx file | Step 5 WebSocket check; the `101` test above |
| Page loads but no video | TV stream offline or URL changed | `check_stream.sh`; update `STREAM_URL` in `.env`, restart the service |
| 403 "Host not allowed" on stream requests | Stream host differs from `STREAM_URL`'s host | Add it to `PROXY_ALLOWED_HOSTS` in `.env`, restart |
| Spanish captions blank | Groq quota exhausted or key invalid | `journalctl -u tvcidade10` for 429/401 |
| App missing after reboot | service not enabled | `sudo systemctl enable tvcidade10` |

---

## Rolling back

```bash
sudo yunohost user permission remove $APP_ID.main visitors   # close it to the public again
sudo systemctl disable --now tvcidade10                      # stop the app
sudo yunohost app remove $APP_ID                             # remove only the proxy app
```

Restoring a backup: `sudo yunohost backup restore <archive name>`.

---

## Security notes

- The app listens on `127.0.0.1` only; the outside world reaches it through nginx.
- It runs as the unprivileged `tv10svc` user, and `.env` is `chmod 600`.
- After Step 7 the player is public by design. Captioning runs once for all viewers and
  pauses when nobody is watching, so more viewers do not mean more Groq calls.
- The HLS proxy only signs and fetches URLs on `STREAM_URL`'s host plus
  `PROXY_ALLOWED_HOSTS`; it is not an open relay.
- Keep `GROQ_API_KEY` out of git and out of shell history. If it leaks, revoke it in the
  Groq console and put a new one in `.env`.
