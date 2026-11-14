# Contrat du connecteur `cortex-ops`

Le connecteur MCP `cortex-ops` n'existe pas encore dans le catalogue AutoClaude. L'agent
`sysops` le référence ; tant qu'il manque, le scan le signale (`missing_refs`) et l'agent
ne peut pas être validé.

Principe : serveur MCP sur Cortex, **lecture seule**, qui expose des outils bornés. Pas de
shell libre, pas d'accès aux fichiers `.env`, aux secrets ni aux données applicatives.

| Outil | Retour |
|-------|--------|
| `os_status` | version Ubuntu, noyau, uptime, date du dernier `apt upgrade` |
| `apt_upgradable` | paquets à mettre à jour, avec marquage sécurité (USN si connu) |
| `reboot_required` | booléen + paquets à l'origine (`/var/run/reboot-required.pkgs`) |
| `disk_usage` | usage par montage (dont `/mnt/synology`), inodes |
| `docker_ps` | conteneurs : état, santé, nombre de redémarrages, uptime |
| `docker_image_versions` | image:tag et digest en cours, par stack compose |
| `service_health` | état des services natifs (qBittorrent, OpenClaw, Jeedom…) |
| `journal_errors` | erreurs récentes agrégées, sans contenu sensible |
| `backup_status` | dernière sauvegarde réussie par cible (scripts/backup-to-synology.sh) |
| `cert_expiry` | expiration des certificats Traefik |

Les variables d'environnement des conteneurs ne doivent jamais être renvoyées.
