# Adding a new service

## Service running in Docker (on the `proxy` network)

The Docker provider is opt-in (`exposedByDefault: false` in `traefik.yml`) — a container
is only picked up if it carries `traefik.enable=true`. Add labels to the service in
`docker-compose.yml`:

```yaml
  myapp:
    image: myapp:latest
    networks:
      - proxy
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.myapp.rule=Host(`myapp.jaganin.duckdns.org`)"
      - "traefik.http.routers.myapp.entrypoints=websecure"
      - "traefik.http.routers.myapp.tls.certresolver=duckdns"
      - "traefik.http.routers.myapp.middlewares=authelia@file,secure-headers@file"
      - "traefik.http.services.myapp.loadbalancer.server.port=8080"
```

`authelia` and `secure-headers` are defined in `traefik/dynamic/middlewares.yml` (file
provider), so a docker-provider router must reference them with the `@file` suffix —
Traefik requires the provider namespace explicitly whenever it differs from the
router's own provider.

Only containers that actually run on Cortex's `proxy` Docker network are candidates for
labels. Native services (Jeedom, qBittorrent, Synology, Alfred...) stay in the file
provider below — there's no container for the Docker provider to attach labels to.

### Blue/green deployments

Multiple containers carrying the **same** `traefik.http.services.<name>.loadbalancer.*`
label are aggregated into a single load balancer with one backend per container —
confirmed by running two identically-labeled containers and checking
`/api/http/services/<name>@docker` lists both. This is what makes zero-downtime
deploys possible: start the new container (same service label) alongside the old one,
wait for it to be healthy, then stop the old one — Traefik never drops traffic.

### MyCGP — ephemeral PR previews (Jaganin/MyCGP#298)

MyCGP runs 3 environment tiers, all reached via the Docker provider (labels set by
MyCGP's own deploy/teardown script, out of scope here):

- `mycgp.jaganin.duckdns.org` — prod, branch `main`
- `preview.mycgp.jaganin.duckdns.org` — UAT, branch `preview`
- `pr-<n>.preview.mycgp.jaganin.duckdns.org` — one throwaway instance per open PR

The `preview` and `pr-<n>` tiers share a single wildcard cert
(`*.preview.mycgp.jaganin.duckdns.org`, pre-provisioned in `traefik/traefik.yml`)
instead of Traefik requesting a fresh cert per PR — that would risk hitting Let's
Encrypt's 5 duplicate-certs/week rate limit if several PRs open and close in the same
week. `mycgp.jaganin.duckdns.org` keeps its own existing cert, unchanged.

Expected labels for a per-PR container, Host rule derived from the PR number:

```yaml
  mycgp-pr-<n>:
    image: mycgp:pr-<n>
    networks:
      - proxy
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.mycgp-pr-<n>.rule=Host(`pr-<n>.preview.mycgp.jaganin.duckdns.org`)"
      - "traefik.http.routers.mycgp-pr-<n>.entrypoints=websecure"
      - "traefik.http.routers.mycgp-pr-<n>.tls.certresolver=duckdns"
      - "traefik.http.routers.mycgp-pr-<n>.middlewares=secure-headers@file"
      - "traefik.http.services.mycgp-pr-<n>.loadbalancer.server.port=8020"
```

No `tls.domains` override needed on the router — the wildcard cert covering
`*.preview.mycgp.jaganin.duckdns.org` is already provisioned at the entrypoint level,
so Traefik serves it for any matching SNI without a new ACME request. Same for the
`preview` (UAT) router itself. Reuse `secure-headers@file` (defined in
`traefik/dynamic/middlewares.yml`) like every other Docker-provider router; no
`authelia` middleware — MyCGP handles its own auth.

## Service running natively on Cortex

Add a router + service block in `traefik/dynamic/services.yml`:

```yaml
http:
  services:
    myapp:
      loadBalancer:
        servers:
          - url: "http://localhost:<PORT>"

  routers:
    myapp:
      rule: "Host(`myapp.jaganin.duckdns.org`)"
      entrypoints:
        - websecure
      tls:
        certResolver: duckdns
      middlewares:
        - authelia
        - secure-headers
      service: myapp
```

Traefik hot-reloads dynamic config — no restart needed.

## Bypassing auth for a service

In `authelia/configuration.yml`, add a bypass rule **before** the wildcard rule:

```yaml
access_control:
  rules:
    - domain: "myapp.jaganin.duckdns.org"
      policy: bypass
    - domain: "*.jaganin.duckdns.org"
      policy: two_factor
```

Remove the `authelia` middleware from the Traefik router as well.

## AutoClaude — identité Authelia et secret de proxy

L'orchestrateur AutoClaude réserve ses routes sensibles (celles qui modifient quelque
chose) aux requêtes **passées par Authelia**. Il ne peut pas se fier au seul en-tête
`Remote-User` : ses ports `8000` et `3001` sont aussi joignables directement sur l'hôte,
sans passer par Traefik (notamment depuis les conteneurs d'agents via
`host.docker.internal`), donc n'importe qui pourrait envoyer un faux `Remote-User`.

Traefik ajoute donc un **secret partagé** dans l'en-tête `X-Proxy-Auth`, uniquement sur
les requêtes validées par Authelia. Deux middlewares dans `traefik/dynamic/middlewares.yml` :

- `autoclaude-proxy-auth` : ajoute `X-Proxy-Auth: <secret>`. Le secret est lu depuis la
  variable d'environnement `AUTOCLAUDE_PROXY_AUTH_SECRET` du conteneur Traefik grâce au
  templating Go du provider fichier (`{{ env "AUTOCLAUDE_PROXY_AUTH_SECRET" }}`) : il
  n'est jamais écrit dans le repo. Placé **après** `authelia` sur le routeur `autoclaude`
  (`authelia`, `autoclaude-proxy-auth`, `secure-headers`). Le middleware `headers` écrase
  la valeur, donc un `X-Proxy-Auth` envoyé par le client est remplacé.
- `autoclaude-strip-identity` : met à `""` (donc supprime) `X-Proxy-Auth`, `Remote-User`,
  `Remote-Groups`, `Remote-Name` et `Remote-Email`. Appliqué aux routeurs qui contournent
  Authelia — `autoclaude-widget`, `autoclaude-credentials` et `autoclaude-mcp` — pour
  qu'un client ne puisse jamais y faire passer une identité.

Le secret se génère avec `openssl rand -hex 32` et doit être **identique des deux
côtés** :

- `AUTOCLAUDE_PROXY_AUTH_SECRET` dans le `.env` de cortex-internet (transmis à Traefik
  par `docker-compose.yml`) ;
- `PROXY_AUTH_SECRET` dans le `.env` de l'orchestrateur AutoClaude.

Si la variable est absente côté Traefik, l'en-tête est envoyé vide (donc supprimé) et
l'orchestrateur refuse les routes sensibles. Voir `docs/deployment.md` pour la mise en
service.

## SSO via OIDC (preferred over ForwardAuth)

When a service supports OAuth2/OIDC natively, configure Authelia as the OIDC provider
instead of using ForwardAuth. This gives a real single sign-on experience (one Authelia
login covers all OIDC-enabled services).

### 1. Register the client in `authelia/configuration.yml`

```yaml
identity_providers:
  oidc:
    # hmac_secret via AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET env var
    jwks:
      - key_id: main
        algorithm: RS256
        use: sig
        key: |
          -----BEGIN PRIVATE KEY-----
          <RSA 2048 private key — generate with: openssl genrsa 2048>
          -----END PRIVATE KEY-----
    clients:
      - client_id: myapp
        client_name: MyApp
        # Hash with: docker run --rm authelia/authelia:latest authelia crypto hash generate argon2 --password <secret>
        client_secret: '$argon2id$...'
        public: false
        authorization_policy: two_factor
        consent_mode: implicit
        pre_configured_consent_duration: 1y
        redirect_uris:
          - https://myapp.jaganin.duckdns.org/auth/login
        scopes:
          - openid
          - profile
          - email
        token_endpoint_auth_method: client_secret_post
```

Add a bypass rule for the service (Authelia ForwardAuth not needed — the service
handles auth via OIDC):

```yaml
access_control:
  rules:
    - domain: "myapp.jaganin.duckdns.org"
      policy: bypass
```

### 2. Traefik router — no `authelia` middleware

```yaml
  routers:
    myapp:
      middlewares:
        - secure-headers   # authelia NOT here — OIDC handles it
      service: myapp
```

### 3. Configure the service side

Point the service's OAuth settings to:
- **Issuer URL**: `https://auth.jaganin.duckdns.org`
- **Client ID / Secret**: as defined above

### Notes — Paperless-ngx OIDC specifics (allauth 65.x)

- Use `client_secret_basic` in the Authelia client (allauth default — unlike most other clients that use `client_secret_post`)
- Redirect URI: `https://paperless.jaganin.duckdns.org/accounts/oidc/authelia/login/callback/`
- The OIDC provider module is **not** included by default: add `PAPERLESS_APPS: allauth.socialaccount.providers.openid_connect`
- Configure via env:
  ```yaml
  PAPERLESS_APPS: allauth.socialaccount.providers.openid_connect
  PAPERLESS_REDIRECT_LOGIN_TO_SSO: "true"
  PAPERLESS_DISABLE_REGULAR_LOGIN: "true"
  PAPERLESS_ACCOUNT_ALLOW_SIGNUPS: "false"
  PAPERLESS_SOCIAL_AUTO_SIGNUP: "false"
  PAPERLESS_SOCIALACCOUNT_PROVIDERS: >-
    {"openid_connect": {"APPS": [{"provider_id": "authelia", "name": "Authelia",
    "client_id": "paperless", "secret": "<plain_secret>",
    "settings": {"server_url": "https://auth.jaganin.duckdns.org"}}]}}
  ```
- Linking OIDC to an existing user: allauth 65.x does not auto-connect by email.
  Workaround: temporarily enable signups, complete the OIDC flow once (creates a temp user),
  then in Django shell transfer the `SocialAccount` to the real user and delete the temp user.

### Notes — Nextcloud OIDC specifics

- Include the `groups` scope in the client definition (Nextcloud's `user_oidc` requests it)
- Redirect URI: `https://nextcloud.jaganin.duckdns.org/apps/user_oidc/code`
- App: **OpenID Connect user backend** (`user_oidc`), discovery endpoint: `https://auth.jaganin.duckdns.org/.well-known/openid-configuration`
- Homepage widget must use the **public HTTPS URL** (`https://nextcloud.jaganin.duckdns.org`), not the internal HTTP port — Nextcloud enforces `Secure` cookies when `overwriteprotocol: https` is set
- Configure `trusted_proxies` to avoid brute-force throttling on the Docker/LAN range:
  ```bash
  occ config:system:set trusted_proxies 0 --value=172.16.0.0/12
  occ config:system:set trusted_proxies 1 --value=192.168.0.0/16
  ```

---

## Services overview

| Service | URL | ForwardAuth | SSO OIDC | Notes |
|---------|-----|:-----------:|:--------:|-------|
| Jeedom | `jaganin.duckdns.org` / `jeedom.` | ❌ | ❌ | Auth Apache/Jeedom |
| Authelia | `auth.` | — | — | Portail auth |
| Traefik dashboard | `traefik.` | ✅ | ❌ | |
| qBittorrent | `qbittorrent.` | ✅ | ❌ | |
| Alfred | `alfred.` | ✅ | ❌ | |
| Paperless | `paperless.` | ❌ bypass | ✅ | SSO Authelia OIDC (allauth 65.x) |
| OpenHands | `openhands.` | ✅ | ❌ | |
| Homepage | `dashboard.` | ✅ | ❌ | |
| Jellyfin | `jellyfin.` | ❌ | ❌ | ⚠️ Accès sans auth |
| Nextcloud | `nextcloud.` | ❌ bypass | ✅ | SSO Authelia OIDC (`user_oidc`) |
| Immich | `photo.` | ❌ bypass | ✅ | SSO Authelia OIDC |
| MyCGP | `mycgp.` | ❌ own auth | ❌ | Docker provider (blue/green), port 8020. UAT (`preview.`) and per-PR (`pr-<n>.preview.`) tiers documented above |
| leboncoin-mcp | `leboncoin.` | ❌ Basic Auth | ❌ | Docker (build from `./lbc-mcp`), port 8040, transport MCP SSE. Basic Auth instead of Authelia — MCP clients can't do interactive TOTP login |
| AutoClaude | `autoclaude.` | ✅ (sauf widget, `/api/credentials`, `/mcp`) | ❌ | Dashboard (3001) et orchestrateur (8000, `/mcp`). Secret `X-Proxy-Auth` ajouté après Authelia, identité supprimée sur les routes qui contournent Authelia — voir plus haut |
| Phoenix | `phoenix.` | ❌ bypass | ✅ | Docker labels (`proxy` network), SSO Authelia OIDC. Container also joins the internal `phoenix` network for OTLP (cortex-phoenix repo) |
