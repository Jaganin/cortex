# Contrat du connecteur `cortex-ops`

Serveur MCP sur Cortex (`cortex-ops/` dans `Jaganin/cortex`, conteneur `cortex-ops`),
déclaré dans le catalogue AutoClaude sous le nom `cortex-ops` (mise en service :
`docs/deployment.md`, section « cortex-ops »).

Principe : **lecture seule**, outils bornés. Aucun outil ne prend d'argument : pas de shell
libre ni de commande construite à partir de l'appelant, pas d'accès aux fichiers `.env`,
aux secrets ni aux données applicatives. Le conteneur ne voit que quelques fichiers de
l'hôte montés en lecture seule, les faits écrits par `scripts/cortex-ops-facts.sh` et
l'API Docker via un proxy qui n'autorise que les `GET` sur les conteneurs et les images.

| Outil | Retour | Source |
|-------|--------|--------|
| `os_status` | version Ubuntu, noyau, uptime, date du dernier `apt upgrade` (unattended ou manuel, nombre de paquets) | `/etc/os-release`, `/proc/uptime`, `/var/log/apt/history.log*` |
| `apt_upgradable` | paquets à mettre à jour, `security: true` s'ils viennent d'une poche `*-security` ; fraîcheur des listes (`lists_updated_at`) | `apt list --upgradable` sur l'état dpkg et les listes apt de l'hôte |
| `reboot_required` | booléen, depuis quand, paquets à l'origine | `/var/run/reboot-required(.pkgs)` recopiés par le collecteur |
| `disk_usage` | usage et inodes par montage (dont `/mnt/synology`, `synology_mounted`) | `df` par le collecteur |
| `docker_ps` | conteneurs : état, santé, nombre de redémarrages, démarrage, projet compose | API Docker (GET) |
| `docker_image_versions` | image:tag, id, digest et label de version en cours, par stack compose | API Docker (GET) |
| `service_health` | joignabilité HTTP des services natifs (qBittorrent, OpenClaw, Jeedom, Synology) | sondes anonymes, liste dans `cortex-ops/config.yml` |
| `journal_errors` | erreurs (priorité ≤ err) des dernières 24 h agrégées par unité, un exemple masqué et tronqué | journal systemd de l'hôte |
| `backup_status` | dernière sauvegarde réussie par cible, `ok` / `stale` / `failed` / `unknown` | `/var/lib/cortex-ops/backups/<cible>.json`, écrit par le script de sauvegarde |
| `cert_expiry` | certificat servi par Traefik par hôte : SAN, émetteur, expiration, jours restants | poignée de main TLS avec `traefik:443` (jamais `acme.json`) |

Les variables d'environnement et les labels (hors `com.docker.compose.*`) des conteneurs
ne sont jamais renvoyés.

## Limites connues

- `apt_upgradable` ne résout pas les identifiants **USN** (pas d'accès réseau aux avis) :
  les chercher sur <https://ubuntu.com/security/notices> pour les paquets marqués
  `security`. Les listes sont celles de l'hôte : si `lists_updated_at` est ancien,
  le signaler plutôt que conclure à l'absence de mises à jour.
- `reboot_required` et `disk_usage` dépendent du collecteur (cron toutes les 5 minutes) :
  `stale: true` signifie que le collecteur ne tourne plus, ce qui est en soi un constat.
- `journal_errors` masque mots de passe, jetons, e-mails et chaînes opaques longues ;
  l'exemple reste un extrait de log, ne pas le recopier tel quel s'il paraît sensible.
- `backup_status` n'a de données que si le script de sauvegarde écrit ses fichiers de
  statut (voir `docs/deployment.md`) ; une cible `unknown` est à signaler.
