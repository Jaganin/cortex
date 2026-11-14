# Deployment Guide

## Prerequisites

- Cortex server (Ubuntu 24.04) with public internet access
- DuckDNS account with token for `jaganin.duckdns.org`
- Ports 80 and 443 forwarded on the Freebox/Synology to Cortex (`192.168.1.120`)

## 1. Port forwarding

On the **Freebox** (or Synology if it's the NAT entry point), add:

| Protocol | External port | Internal IP     | Internal port |
|----------|--------------|-----------------|---------------|
| TCP      | 80           | 192.168.1.120   | 80            |
| TCP      | 443          | 192.168.1.120   | 443           |

## 2. Deploy on Cortex

```bash
# Clone and bootstrap (as root)
git clone https://github.com/Jaganin/cortex.git /opt/cortex-internet
cd /opt/cortex-internet
bash scripts/install.sh
```

The script stops if `.env` is missing. Fill it in:

```bash
nano /opt/cortex-internet/.env
```

Required values:
```bash
DUCKDNS_TOKEN=<your token from duckdns.org>
AUTHELIA_JWT_SECRET=$(openssl rand -hex 32)
AUTHELIA_SESSION_SECRET=$(openssl rand -hex 32)
AUTHELIA_STORAGE_ENCRYPTION_KEY=$(openssl rand -hex 32)
```

Également requis pour AutoClaude (voir « AutoClaude — secret de proxy » plus bas) :
```bash
AUTOCLAUDE_PROXY_AUTH_SECRET=$(openssl rand -hex 32)
```

## 3. Create Authelia user password

```bash
docker run --rm authelia/authelia:latest \
  authelia crypto hash generate argon2 --password 'yourpassword'
```

Copy the `$argon2id$...` hash into `authelia/users_database.yml`, field `password`.

## 4. Set up Authelia TOTP

First start:
```bash
cd /opt/cortex-internet
docker compose up -d
```

Open `https://auth.jaganin.duckdns.org`, log in, and enroll your TOTP device (Google Authenticator / Bitwarden / etc.).

## 5. DuckDNS auto-update (cron)

```bash
chmod +x /opt/cortex-internet/scripts/update-duckdns.sh

# Add to crontab
crontab -e
# Add:
*/5 * * * * /opt/cortex-internet/scripts/update-duckdns.sh >> /var/log/duckdns.log 2>&1
```

## 6. Verify

```bash
# Check all containers are running
docker compose ps

# Check Traefik logs
docker compose logs traefik -f

# Test HTTPS
curl -I https://auth.jaganin.duckdns.org
```

## Additional services

### leboncoin-mcp

MCP server ([wydii/leboncoin-mcp](https://github.com/wydii/leboncoin-mcp)) exposing Leboncoin search to AI assistants. Built as a Docker image from the upstream source (not vendored in this repo — cloned once, gitignored):

```bash
cd /opt/cortex-internet
git clone https://github.com/wydii/leboncoin-mcp.git lbc-mcp
docker compose up -d --build leboncoin-mcp
```

The `dockerfile_inline` in `docker-compose.yml` builds the image directly from `./lbc-mcp` (requirements.txt + server.py). No host port is published — it's reached by Traefik over the `proxy` Docker network only.

Exposed at `https://leboncoin.jaganin.duckdns.org` via Docker labels (`docker-compose.yml`, Traefik's Docker provider), protected by **Traefik Basic Auth** (not Authelia — MCP clients connect over SSE and can't complete Authelia's interactive TOTP login).

Credentials file (gitignored, generate on Cortex):
```bash
mkdir -p /opt/cortex-internet/traefik/secrets
docker run --rm httpd:2.4-alpine htpasswd -nbB <user> '<password>' \
  > /opt/cortex-internet/traefik/secrets/leboncoin-mcp.htpasswd
docker compose up -d traefik   # recreate to pick up the new secrets volume mount
```

The Dockerfile overrides upstream's CLI (`server.py --sse`, legacy transport) and instead calls
`server.mcp.run(transport='http', ...)` directly — modern **streamable-http** transport, served at `/mcp` (not `/sse`).

Client config example (MCP client with streamable-http transport + Basic Auth in the URL or an `Authorization: Basic` header):
```
https://<user>:<password>@leboncoin.jaganin.duckdns.org/mcp
```

To update after an upstream change:
```bash
cd /opt/cortex-internet/lbc-mcp && git pull
cd /opt/cortex-internet && docker compose up -d --build leboncoin-mcp
```

### AutoClaude — secret de proxy

Traefik ajoute l'en-tête `X-Proxy-Auth` sur les requêtes vers
`https://autoclaude.jaganin.duckdns.org` validées par Authelia. L'orchestrateur s'en sert
pour vérifier qu'une requête est bien passée par Authelia, car ses ports `8000` et `3001`
sont aussi joignables directement sur l'hôte. Sur les routes qui contournent Authelia
(widget, `/api/credentials`, `/mcp`), Traefik supprime `X-Proxy-Auth` et les en-têtes
`Remote-*` envoyés par le client. Détails dans `docs/services.md`.

1. Générer le secret :
   ```bash
   openssl rand -hex 32
   ```
2. Le renseigner dans `/opt/cortex-internet/.env` :
   ```bash
   AUTOCLAUDE_PROXY_AUTH_SECRET=<valeur générée>
   ```
3. Renseigner **exactement la même valeur** dans `PROXY_AUTH_SECRET` du `.env` de
   l'orchestrateur AutoClaude, puis redémarrer l'orchestrateur.
4. Recréer Traefik pour qu'il lise la nouvelle variable (le templating
   `{{ env "..." }}` est évalué au chargement de la config dynamique) :
   ```bash
   docker compose up -d traefik
   ```

Le secret n'est jamais versionné : il vit uniquement dans les deux `.env` (gitignorés).
Cette configuration doit être en place, avec le secret renseigné des deux côtés,
**avant** de déployer côté AutoClaude le contrôle de `X-Proxy-Auth`
(Jaganin/AutoClaude#414), qui s'active sans phase de transition.

### cortex-ops — MCP en lecture seule pour l'agent SysOps

Serveur MCP (`cortex-ops/`, conteneurs `cortex-ops` et `cortex-ops-docker`) utilisé par
l'agent AutoClaude `sysops` (`agents/sysops/`). Contrat et sources de chaque outil :
`agents/sysops/knowledge/cortex-ops-contrat.md`. Pas de route Traefik : le port `8050`
n'est publié que sur la passerelle `docker0` (`172.17.0.1`), où l'orchestrateur AutoClaude
le joint ; il n'est accessible ni depuis Internet ni depuis le LAN.

1. Générer le jeton et le renseigner dans `/opt/cortex-internet/.env` :
   ```bash
   echo "CORTEX_OPS_TOKEN=$(openssl rand -hex 32)" >> /opt/cortex-internet/.env
   ```
2. Installer le collecteur de faits de l'hôte (`df`, `reboot-required`), lancé par la
   crontab de root, et le lancer une première fois pour créer les dossiers montés :
   ```bash
   chmod +x /opt/cortex-internet/scripts/cortex-ops-facts.sh
   mkdir -p /var/lib/cortex-ops/backups
   /opt/cortex-internet/scripts/cortex-ops-facts.sh
   crontab -e
   # Add:
   */5 * * * * /opt/cortex-internet/scripts/cortex-ops-facts.sh >> /var/log/cortex-ops-facts.log 2>&1
   ```
3. Démarrer :
   ```bash
   cd /opt/cortex-internet
   docker compose up -d --build cortex-ops
   # 401 attendu sans jeton, la preuve que le serveur répond :
   curl -s -o /dev/null -w '%{http_code}\n' -X POST http://172.17.0.1:8050/mcp
   ```
4. Déclarer le connecteur dans AutoClaude (dashboard, **Agents → Connecteurs**, ou
   `PUT /api/agents/connectors/cortex-ops`, réservé aux humains) :
   - secret `CORTEX_OPS_TOKEN` : la valeur du `.env` ;
   - connecteur `cortex-ops` :
     ```json
     {
       "type": "http",
       "url": "http://172.17.0.1:8050/mcp",
       "headers": { "Authorization": "Bearer ${secret:CORTEX_OPS_TOKEN}" },
       "description": "Cortex en lecture seule (OS, paquets, disques, Docker, services, journal, sauvegardes, certificats) — Jaganin/cortex"
     }
     ```
5. Relancer le scan de `Jaganin/cortex` : `missing_refs` ne doit plus contenir
   `cortex-ops`. Valider puis déployer la révision de l'agent `sysops`, et faire un
   lancement manuel pour vérifier que les 10 outils répondent.

**Sauvegardes** (`backup_status`) : le script de sauvegarde écrit, à la fin de chaque
passage, un fichier `/var/lib/cortex-ops/backups/<cible>.json` par cible. Les cibles
attendues se déclarent dans `cortex-ops/config.yml` (`backups.targets`) pour qu'une cible
sans fichier soit signalée. Exemple à la fin de `backup-to-synology.sh` :
```bash
backup_status() {  # backup_status <cible> ok|failed
  local f=/var/lib/cortex-ops/backups/$1.json now; now=$(date -u +%FT%TZ)
  local last_success; last_success=$(grep -o '"last_success":"[^"]*"' "$f" 2>/dev/null | cut -d'"' -f4 || true)
  [ "$2" = ok ] && last_success=$now
  printf '{"last_attempt":"%s","last_result":"%s","last_success":%s}\n' "$now" "$2" \
    "$( [ -n "$last_success" ] && printf '"%s"' "$last_success" || echo null )" > "$f"
}
```

**Services natifs et certificats** : listes dans `cortex-ops/config.yml`, relu à chaque
appel (pas de redémarrage nécessaire).

## Updates

```bash
cd /opt/cortex-internet
git pull
docker compose pull
docker compose up -d
```
