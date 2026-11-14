#!/bin/bash
# Host facts for the cortex-ops MCP server (Jaganin/cortex#43), run by root's cron every
# 5 minutes. Writes only non-sensitive facts to /var/lib/cortex-ops/facts, which the
# container mounts read-only, so that it needs neither the host root nor /run (which
# holds the Docker socket):
#   - df.txt / df-inodes.txt   space and inodes per filesystem
#   - reboot-required(.pkgs)   mirror of /var/run/reboot-required(.pkgs)
#   - collected_at             touched at the end of each run
set -euo pipefail

OUT=/var/lib/cortex-ops/facts
mkdir -p "$OUT"
chmod 755 /var/lib/cortex-ops "$OUT"

EXCLUDE=(-x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs)
df -B1 "${EXCLUDE[@]}" --output=source,fstype,size,used,avail,target > "$OUT/df.txt.tmp"
df "${EXCLUDE[@]}" --output=target,itotal,iused,iavail > "$OUT/df-inodes.txt.tmp"
mv "$OUT/df.txt.tmp" "$OUT/df.txt"
mv "$OUT/df-inodes.txt.tmp" "$OUT/df-inodes.txt"

for f in reboot-required reboot-required.pkgs; do
  if [ -f "/var/run/$f" ]; then
    # -p keeps the mtime: reboot_required reports since when the reboot is pending
    cp -p "/var/run/$f" "$OUT/$f"
  else
    rm -f "$OUT/$f"
  fi
done

touch "$OUT/collected_at"
