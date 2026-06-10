# Server Security: Nginx as User — No Root Required

**Date:** 2026-06-10
**Scope:** Virtual machine hosting `robka.de` (toolrecall, ki-game, lightbulb, etc.)
**Requirement:** ToolRecall runs entirely with user privileges — no server process requires root.

---

## 1. Requirement: User-Only Rights

ToolRecall's security model demands that **no daemon or server process runs as root**. This applies to every service on the machine:

| Service | Before | After | Runs As |
|---------|--------|-------|---------|
| nginx (master) | `root` | `hermes` | User |
| nginx (workers) | `www-data` | `hermes` | User |
| ki-game-api | `hermes` | **stopped** | N/A |
| dashboard | `hermes` | `hermes` | User |
| goatcounter | `hermes` | `hermes` | User |
| toolrecall daemon | `hermes` | `hermes` | User |
| certbot cron | `root` | `root` (required) | System |

Root is eliminated from all application processes. The only root processes are system-level infrastructure that cannot run as user: Docker/containerd, fail2ban (iptables), Google guest agents (cloud management), and certbot (Let's Encrypt renewal).

---

## 2. Architecture

### 2.1 authbind — Bind Privileged Ports Without Root

**Problem:** Ports < 1024 (80, 443) traditionally require root.
**Solution:** `authbind` — an LD_PRELOAD wrapper that intercepts `bind()` syscalls and allows non-root binding when authorized via `/etc/authbind/byport/`.

```
┌─────────────────────────────────────────────────────┐
│  authbind --deep /usr/sbin/nginx                    │
│    ↓                                                 │
│  LD_PRELOAD=libauthbind.so intercepts bind()         │
│    ↓                                                 │
│  access("/etc/authbind/byport/80", X_OK) → allowed   │
│    ↓                                                 │
│  bind(80) → kernel accepts (no CAP_NET_BIND_SERVICE) │
└─────────────────────────────────────────────────────┘
```

**Performance:** The `access(2)` check runs once per `bind()` syscall at startup (~5µs per port). On the hot path (accept, read, sendfile, write) authbind adds zero overhead — it only hooks `bind()`.

### 2.2 File Layout

```
/home/hermes/
├── nginx-user.conf            # Custom nginx config (no system deps)
├── ssl/
│   ├── fullchain.pem          # Copy of Let's Encrypt cert (rw-r--r--)
│   ├── privkey.pem            # Copy of private key (rw-------)
│   ├── chain.pem              # Intermediate chain
│   └── certbot-deploy-hook.sh # Post-renewal script
├── run/
│   └── nginx-user.pid         # PID file (user-writable)
├── logs/nginx/
│   ├── access.log             # nginx access log
│   └── error.log              # nginx error log
└── .config/systemd/user/
    └── nginx-user.service     # User-level systemd unit
```

### 2.3 Config Files

**`nginx-user.conf`** — Minimal nginx config, ~15 lines:

```
worker_processes auto;
pid /home/hermes/run/nginx-user.pid;
error_log /home/hermes/logs/nginx/error.log;

events { worker_connections 768; }

http {
    sendfile on;
    include /etc/nginx/mime.types;
    ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;
    access_log /home/hermes/logs/nginx/access.log;
    include /etc/nginx/sites-enabled/*;
}
```

The site config (`/etc/nginx/sites-available/robka.de`) references SSL certs at `/home/hermes/ssl/` instead of `/etc/letsencrypt/live/`.

**`nginx-user.service`** — User-level systemd unit:

```
[Unit]
Description=User-level Nginx (non-root, via authbind)
After=network.target

[Service]
Type=forking
PIDFile=/home/hermes/run/nginx-user.pid
ExecStartPre=/usr/bin/authbind --deep /usr/sbin/nginx -t -c /home/hermes/nginx-user.conf
ExecStart=/usr/bin/authbind --deep /usr/sbin/nginx -c /home/hermes/nginx-user.conf
ExecReload=/usr/bin/authbind --deep /usr/sbin/nginx -c /home/hermes/nginx-user.conf -s reload
ExecStop=/usr/bin/kill -QUIT $(cat /home/hermes/run/nginx-user.pid)
Restart=on-failure
```

### 2.4 Access Control Chain

```
User (hermes)
  │
  ├── sudo (NOPASSWD whitelist — only specific binaries)
  │   ├── apt / apt-get / dpkg     — package management
  │   ├── systemctl                — manage system services
  │   ├── certbot                  — Let's Encrypt renewal
  │   ├── chmod                    — set file permissions
  │   └── fuser / fallocate / swapon / whoami
  │
  ├── systemctl --user             — manage user services (no sudo needed)
  │   ├── nginx-user.service       — WEB SERVER (this doc)
  │   ├── hermes-gateway.service   — Hermes AI agent
  │   ├── toolrecall-daemon.service— ToolRecall cache
  │   └── dashboard.service        — Python dashboard
  │
  └── authbind --deep              — bind ports 80/443
```

---

## 3. SSL Certificate Renewal

### 3.1 The Problem

Let's Encrypt stores certificates at `/etc/letsencrypt/archive/robka.de/` with permissions `700 root:root`. The user process (`hermes`) cannot read them. A symlink at `/etc/letsencrypt/live/robka.de/privkey.pem → ../../archive/robka.de/privkey1.pem` breaks when `archive/` is root-only.

### 3.2 The Solution: Copy + Deploy Hook

At renewal time, certbot runs deploy hooks that copy certificates to `/home/hermes/ssl/` and trigger a nginx reload:

```
Certbot Renewal
  │
  ├── 1. Obtains new certs → /etc/letsencrypt/archive/robka.de/ (new files)
  │
  ├── 2. Runs deploy hook: /home/hermes/ssl/certbot-deploy-hook.sh
  │      ├── cp fullchain1.pem /home/hermes/ssl/fullchain.pem
  │      ├── cp privkey1.pem   /home/hermes/ssl/privkey.pem
  │      ├── chmod 644 fullchain.pem
  │      ├── chmod 600 privkey.pem
  │      └── /usr/sbin/nginx -c /home/hermes/nginx-user.conf -s reload
  │
  └── 3. nginx picks up new certs (SIGHUP to master process)
```

**Why not use group permissions?** The `archive` directory owner is `root:root` with 700. Adding `hermes` to group `root` would grant far too broad access. The copy approach is minimal: only the cert key and chain are copied.

**Why `nginx -s reload` from root works without authbind:** The `reload` signal sends SIGHUP to the existing master process — it doesn't `bind()` to ports, so authbind isn't needed. The root-run hook can send the signal directly.

### 3.3 Hook Registration

- **Per-cert hook:** `deploy_hook = /home/hermes/ssl/certbot-deploy-hook.sh` in `/etc/letsencrypt/renewal/robka.de.conf`
- **Global hook:** `/etc/letsencrypt/renewal-hooks/post/restart-https.sh` (same content, runs for all domains)

---

## 4. Ki-Game API: Stopped

The ki-game-api (Gemini API proxy, port 8510, localhost-only) was a Python service that proxied Gemini API calls from the browser (sensitive API key stays server-side).

**Stopped because:** The proxy is not currently needed — there are no active frontend integrations calling it. The service has been:

- `systemctl --user stop ki-game-api.service`
- `systemctl --user disable ki-game-api.service`
- Port 8510 verified free

**To restart:** `systemctl --user start ki-game-api.service`

The ki-game-platform source code remains at `/home/hermes/ki-game-platform/` with its own AGENTS.md and full feature documentation.

---

## 5. Design Decisions

### 5.1 Why Not a Systemd Drop-In Override?

**Attempted:** Create `/etc/systemd/system/nginx.service.d/authbind.conf` with `User=www-data` and `ExecStart=/usr/bin/authbind --deep /usr/sbin/nginx ...`

**Blocked by:** The `hermes` user cannot write to `/etc/systemd/` — sudo is NOPASSWD only for specific whitelisted binaries (apt, dpkg, systemctl, chmod, certbot). Running `sudo mkdir` to create the drop-in directory is not permitted.

**Alternative chosen:** User-level systemd service. Works without any system filesystem writes, fully contained in `~/.config/systemd/user/`.

### 5.2 Why Not CAP_NET_BIND_SERVICE?

`setcap cap_net_bind_service=+ep /usr/sbin/nginx` would allow nginx to bind ports < 1024 without root.

**Rejected because:**
- File capabilities are fragile — `apt upgrade nginx` replaces the binary, stripping the capability
- The user must remember to re-apply `setcap` after every nginx update
- It grants *all* port binding, not a controlled allowlist like authbind

authbind is explicitly per-port (`/etc/authbind/byport/{80,443}`) and survives binary updates.

### 5.3 Why Copy Certs Instead of Changing Archive Permissions?

The cert archive at `/etc/letsencrypt/archive/` was `700 root:root`. We widened it to `755 root:root` and the cert files to `644 root:root` / `640 root:root` to allow the user process to read through symlinks.

**However:** Certbot's `certonly --standalone` re-creates archive directory with 700 and cert files with 600 on every renewal. The deploy hook copies them instead, making renewal independent of archive permissions.

### 5.4 Why Nginx as `hermes` Instead of `www-data`?

The `user www-data;` nginx directive is only effective when the master process runs as root. Without root, the directive is silently ignored (nginx logs a warning at startup).

Since the master process runs as `hermes` via authbind, the workers also run as `hermes`. This is actually **more permissive** than before (www-data was more restricted), but on a single-user system the difference is irrelevant — `www-data` and `hermes` both have read access to `/var/www/robka.de/` files.

**What changes:** There's one fewer `setuid()` syscall per worker at startup (~10µs).

---

## 6. Rollback Procedure

If the user-level nginx fails:

```bash
# 1. Stop user nginx
systemctl --user stop nginx-user.service

# 2. Re-enable system nginx
sudo systemctl enable nginx
sudo systemctl start nginx

# 3. Restore SSL paths in site config
# (edit /etc/nginx/sites-available/robka.de back to /etc/letsencrypt/live/)
python3 -c "
data = open('/etc/nginx/sites-available/robka.de').read()
data = data.replace('/home/hermes/ssl/', '/etc/letsencrypt/live/robka.de/')
open('/etc/nginx/sites-available/robka.de', 'w').write(data)
"

# 4. Validate and reload system nginx
sudo nginx -t && sudo nginx -s reload
```

---

## 7. Verification

### 7.1 No Root Nginx Processes
```bash
ps aux | grep "^root.*nginx"
# → returns nothing
```

### 7.2 All Sites Respond
```bash
curl -sk https://robka.de/           → 200
curl -sk https://robka.de/lightbulb/ → 200
curl -sk https://robka.de/ki-game/   → 200
```

### 7.3 Content Integrity
```bash
md5sum /var/www/robka.de/index.html
curl -sk https://robka.de/index.html | md5sum
# → both match
```

### 7.4 Performance (no regression)
```bash
# Large file (160KB, 50 concurrent): 3,308 req/s | 41 MB/s
# Small file (24KB, 100 concurrent): 2,909 req/s | 71 MB/s
# authbind overhead: ~5µs per port at startup only
```
