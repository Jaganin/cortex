# cortex

> Anciennement `cortex-internet`. Le dossier de déploiement sur Cortex reste `/opt/cortex-internet`.

Secure internet exposure of Cortex services via **Traefik** (reverse proxy) and **Authelia** (SSO + 2FA authentication).

## Overview

All services are exposed under `*.jaganin.duckdns.org` with:
- Automatic HTTPS via Let's Encrypt (DNS-01 challenge with DuckDNS)
- SSO authentication via Authelia (with TOTP 2FA)
- Centralized routing via Traefik

## Architecture

```
Internet
   │
   ▼
[DuckDNS: *.jaganin.duckdns.org] → Cortex public IP
   │
   ▼
[Traefik :443]  ← Let's Encrypt (wildcard cert)
   │
   ├─ [Authelia] auth.jaganin.duckdns.org
   │       └── SSO portal (TOTP 2FA)
   │
   ├─ [qBittorrent] qbittorrent.jaganin.duckdns.org  (protected)
   ├─ [Alfred]      alfred.jaganin.duckdns.org        (protected)
   └─ [Traefik UI]  traefik.jaganin.duckdns.org       (protected)
```

## Stack

| Component  | Role                          | Port  |
|------------|-------------------------------|-------|
| Traefik v3 | Reverse proxy + TLS           | 80, 443 |
| Authelia   | SSO + 2FA (TOTP)              | 9091  |
| Redis      | Authelia session store        | 6379  |

## Services exposed

| Subdomain                         | Backend                     | Auth |
|-----------------------------------|-----------------------------|------|
| `auth.jaganin.duckdns.org`        | Authelia portal             | —    |
| `traefik.jaganin.duckdns.org`     | Traefik dashboard           | ✅   |
| `qbittorrent.jaganin.duckdns.org` | qBittorrent (localhost:8080)| ✅   |
| `alfred.jaganin.duckdns.org`      | OpenClaw Alfred (port 18789)| ✅   |
| `mycgp.jaganin.duckdns.org`       | MyCGP (localhost:8020)      | ✅   |
| `leboncoin.jaganin.duckdns.org`   | leboncoin-mcp (Docker, port 8040) | ✅ |

## Quick start

See [docs/deployment.md](docs/deployment.md) for full installation instructions.

```bash
# On Cortex (as root)
cd /opt/cortex-internet
cp .env.example .env
# Edit .env with your values
nano .env
docker compose up -d
```

## Directory structure

```
cortex/
├── docker-compose.yml        # Main stack
├── .env.example              # Environment variables template
├── traefik/
│   ├── traefik.yml           # Static configuration
│   └── dynamic/
│       ├── middlewares.yml   # Authelia middleware + security headers
│       └── services.yml      # Native/non-Docker service routes (file provider)
├── authelia/
│   ├── configuration.yml     # Authelia config
│   └── users_database.yml    # Local user database (bcrypt)
├── scripts/
│   ├── install.sh            # Bootstrap script (run as root)
│   └── update-duckdns.sh     # DuckDNS IP updater (cron)
├── agents/
│   └── sysops/               # Agent AutoClaude SysOps (patchs OS, montées de version)
└── docs/
    ├── architecture.md       # Detailed architecture
    ├── deployment.md         # Step-by-step deployment guide
    └── services.md           # Adding new services
```
