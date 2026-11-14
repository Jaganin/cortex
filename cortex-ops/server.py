"""cortex-ops — read-only MCP server describing the state of Cortex (Jaganin/cortex#43).

Used by the AutoClaude SysOps agent (agents/sysops) through the `cortex-ops` connector.
Contract: agents/sysops/knowledge/cortex-ops-contrat.md.

Every tool is bounded and read-only: no free shell, no command built from caller input
(no tool takes any argument), no access to `.env`, secrets or application data, and
container environment variables are never returned. The container only sees:

- a few host files mounted read-only (os-release, dpkg/apt state, apt and systemd logs);
- the facts written by scripts/cortex-ops-facts.sh (df, reboot-required) and the backup
  status files, both under /var/lib/cortex-ops;
- the Docker API through a socket proxy that only allows GET on containers and images.
"""

import gzip
import hmac
import http.client
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from cryptography import x509
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

logger = logging.getLogger("cortex-ops")

HOST = Path(os.environ.get("CORTEX_OPS_HOST_ROOT", "/host"))
FACTS_DIR = HOST / "facts"
BACKUPS_DIR = HOST / "backups"
CONFIG_FILE = Path(os.environ.get("CORTEX_OPS_CONFIG", "/app/config.yml"))
DOCKER_HOST = os.environ.get("CORTEX_OPS_DOCKER_HOST", "cortex-ops-docker:2375")
# Facts older than this are flagged as stale (the collector runs every 5 minutes).
FACTS_MAX_AGE = timedelta(minutes=30)
PROBE_TIMEOUT = 5

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

mcp = MCPServer(
    name="cortex-ops",
    instructions="Read-only view of the Cortex host: OS, packages, disks, Docker, services, "
                 "journal, backups and certificates. No tool changes anything.",
)


def _config() -> dict:
    try:
        return yaml.safe_load(CONFIG_FILE.read_text()) or {}
    except FileNotFoundError:
        return {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: float | datetime | None) -> str | None:
    if ts is None:
        return None
    if not isinstance(ts, datetime):
        ts = datetime.fromtimestamp(ts, timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def _facts_age() -> dict:
    """When the host collector last ran, and whether that is too old to be trusted."""
    stamp = FACTS_DIR / "collected_at"
    if not stamp.exists():
        return {"collected_at": None, "stale": True}
    collected = datetime.fromtimestamp(stamp.stat().st_mtime, timezone.utc)
    return {"collected_at": _iso(collected), "stale": _now() - collected > FACTS_MAX_AGE}


# ── Sanitizing ────────────────────────────────────────────────────────────────

_SENSITIVE = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
    # key=value / key: value where the key looks like a credential
    (re.compile(r"(?i)\b([\w.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|auth\w*|cookie|session)[\w.-]*)"
                r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2***"),
    (re.compile(r"(?i)(\w+://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***@***"),
    # long opaque strings (hex, base64, JWT...)
    (re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b"), "***"),
]


def sanitize(text: str, limit: int = 200) -> str:
    """Mask anything that looks like a credential, an email or an opaque token, then
    truncate. Applied to every free text coming from logs."""
    for pattern, repl in _SENSITIVE:
        text = pattern.sub(repl, text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── OS ────────────────────────────────────────────────────────────────────────

def parse_os_release(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


def parse_apt_history(text: str) -> list[dict]:
    """apt history.log entries (oldest first) that upgraded or installed packages:
    end date, how it was started and the package count. Command lines are not returned."""
    entries, current = [], {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        if key == "Start-Date":
            current = {"start": value.strip()}
        elif key == "Commandline":
            current["unattended"] = "unattended-upgrade" in value
        elif key in ("Upgrade", "Install"):
            # "pkg:arch (old, new), pkg2:arch (new)"
            current.setdefault("counts", {})[key.lower()] = len(re.findall(r"\)(?:,|$)", value.strip()))
        elif key == "End-Date" and current.get("counts"):
            entries.append({
                "date": value.strip(),
                "unattended": current.get("unattended", False),
                "upgraded": current["counts"].get("upgrade", 0),
                "installed": current["counts"].get("install", 0),
            })
    return entries


def _last_apt_upgrade() -> dict | None:
    """Most recent apt run that upgraded packages, looking into rotated logs if needed."""
    log_dir = HOST / "var/log/apt"
    files = sorted(log_dir.glob("history.log*"), key=lambda p: (p.name != "history.log", _rotation(p)))
    for path in files:
        try:
            text = gzip.decompress(path.read_bytes()).decode(errors="replace") if path.suffix == ".gz" \
                else path.read_text(errors="replace")
        except OSError:
            continue
        upgrades = [e for e in parse_apt_history(text) if e["upgraded"]]
        if upgrades:
            return upgrades[-1]
    return None


def _rotation(path: Path) -> int:
    match = re.search(r"\.(\d+)", path.name)
    return int(match.group(1)) if match else 0


@mcp.tool(annotations=READ_ONLY)
def os_status() -> dict:
    """Ubuntu version, running kernel, uptime and the last apt run that upgraded packages
    (date, unattended-upgrades or manual, package count)."""
    release = parse_os_release((HOST / "etc/os-release").read_text())
    uptime_s = float(Path("/proc/uptime").read_text().split()[0])
    return {
        "os": release.get("PRETTY_NAME"),
        "version_id": release.get("VERSION_ID"),
        "codename": release.get("VERSION_CODENAME"),
        "kernel": os.uname().release,
        "uptime_seconds": int(uptime_s),
        "booted_at": _iso(time.time() - uptime_s),
        "last_apt_upgrade": _last_apt_upgrade(),
    }


# ── Packages ──────────────────────────────────────────────────────────────────

_APT_LINE = re.compile(r"^(?P<name>[^/\s]+)/(?P<suites>\S+)\s+(?P<new>\S+)\s+(?P<arch>\S+)\s+"
                       r"\[upgradable from: (?P<old>[^\]]+)\]")


def parse_apt_upgradable(text: str) -> list[dict]:
    packages = []
    for line in text.splitlines():
        match = _APT_LINE.match(line.strip())
        if not match:
            continue
        suites = match["suites"].split(",")
        packages.append({
            "name": match["name"],
            "current": match["old"],
            "candidate": match["new"],
            "arch": match["arch"],
            "suites": suites,
            "security": any(s.endswith("-security") for s in suites),
        })
    return packages


@mcp.tool(annotations=READ_ONLY)
def apt_upgradable() -> dict:
    """Packages with a newer candidate version, flagged `security` when it comes from a
    *-security pocket. Computed from the host's dpkg status and apt lists as refreshed by
    the host itself (`lists_updated_at`); USN identifiers are not resolved offline, look
    them up on ubuntu.com/security/notices for the flagged packages."""
    lists = HOST / "var/lib/apt/lists"
    cmd = [
        "apt", "list", "--upgradable",
        "-o", f"Dir::State::status={HOST}/var/lib/dpkg/status",
        "-o", f"Dir::State::Lists={lists}",
        "-o", f"Dir::Etc::sourceparts={HOST}/etc/apt/sources.list.d",
        "-o", "Dir::Etc::sourcelist=/app/empty.list",
        "-o", "Dir::Cache=/tmp/apt-cache",
        "-o", "Debug::NoLocking=1",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"apt list failed: {sanitize(result.stderr, 500)}")
    packages = parse_apt_upgradable(result.stdout)
    newest = max((p.stat().st_mtime for p in lists.glob("*Release")), default=None)
    return {
        "lists_updated_at": _iso(newest),
        "count": len(packages),
        "security_count": sum(p["security"] for p in packages),
        "packages": packages,
    }


@mcp.tool(annotations=READ_ONLY)
def reboot_required() -> dict:
    """Whether the host asks for a reboot (/var/run/reboot-required) and the packages
    behind it (/var/run/reboot-required.pkgs), as mirrored by the host collector."""
    flag = FACTS_DIR / "reboot-required"
    pkgs = FACTS_DIR / "reboot-required.pkgs"
    return {
        "reboot_required": flag.exists(),
        "since": _iso(flag.stat().st_mtime) if flag.exists() else None,
        "packages": sorted(set(pkgs.read_text().split())) if pkgs.exists() else [],
        **_facts_age(),
    }


# ── Disks ─────────────────────────────────────────────────────────────────────

def parse_df(text: str, inodes: str) -> list[dict]:
    """`df -B1 --output=source,fstype,size,used,avail,target` and
    `df --output=target,itotal,iused,iavail` from the host collector."""
    inode_rows = {}
    for line in inodes.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            total, used, free = (int(v) if v.isdigit() else None for v in parts[-3:])
            inode_rows[" ".join(parts[:-3])] = (total, used, free)
    mounts = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or not all(v.isdigit() for v in parts[2:5]):
            continue
        size, used, avail = (int(v) for v in parts[2:5])
        target = " ".join(parts[5:])
        itotal, iused, ifree = inode_rows.get(target, (None, None, None))
        mounts.append({
            "mount": target,
            "source": parts[0],
            "fstype": parts[1],
            "size_bytes": size,
            "used_bytes": used,
            "avail_bytes": avail,
            "used_pct": round(100 * used / (used + avail), 1) if used + avail else None,
            "inodes_total": itotal,
            "inodes_used": iused,
            "inodes_used_pct": round(100 * iused / itotal, 1) if itotal else None,
        })
    return mounts


@mcp.tool(annotations=READ_ONLY)
def disk_usage() -> dict:
    """Space and inode usage per mounted filesystem (including /mnt/synology), from the
    host collector. `used_pct` is computed like df (used / (used + available))."""
    try:
        mounts = parse_df((FACTS_DIR / "df.txt").read_text(), (FACTS_DIR / "df-inodes.txt").read_text())
    except FileNotFoundError:
        mounts = []
    targets = {m["mount"] for m in mounts}
    return {"mounts": mounts, "synology_mounted": "/mnt/synology" in targets, **_facts_age()}


# ── Docker ────────────────────────────────────────────────────────────────────

def _docker_get(path: str) -> Any:
    """GET on the Docker API through the socket proxy (which refuses anything else)."""
    host, _, port = DOCKER_HOST.partition(":")
    conn = http.client.HTTPConnection(host, int(port or 2375), timeout=10)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
    finally:
        conn.close()
    if response.status != 200:
        raise RuntimeError(f"Docker API {path}: HTTP {response.status}")
    return json.loads(body)


def _inspect_all() -> list[dict]:
    return [_docker_get(f"/containers/{c['Id']}/json") for c in _docker_get("/containers/json?all=1")]


def _compose(labels: dict | None) -> tuple[str | None, str | None]:
    labels = labels or {}
    return labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")


def container_summary(inspect: dict) -> dict:
    """Public view of a container: never Config.Env, nor any label beyond compose's."""
    state = inspect.get("State") or {}
    project, service = _compose((inspect.get("Config") or {}).get("Labels"))
    started = state.get("StartedAt")
    return {
        "name": (inspect.get("Name") or "").lstrip("/"),
        "image": (inspect.get("Config") or {}).get("Image"),
        "compose_project": project,
        "compose_service": service,
        "state": state.get("Status"),
        "health": (state.get("Health") or {}).get("Status"),
        "restart_count": inspect.get("RestartCount", 0),
        "restart_policy": ((inspect.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
        "started_at": started if state.get("Running") else None,
        "finished_at": None if state.get("Running") else state.get("FinishedAt"),
        "exit_code": None if state.get("Running") else state.get("ExitCode"),
        "oom_killed": state.get("OOMKilled", False),
    }


@mcp.tool(annotations=READ_ONLY)
def docker_ps() -> dict:
    """All containers (running or not): state, health, restart count, start time and
    compose project. Environment variables and labels are never returned."""
    containers = sorted((container_summary(c) for c in _inspect_all()), key=lambda c: c["name"])
    return {
        "count": len(containers),
        "running": sum(c["state"] == "running" for c in containers),
        "unhealthy": [c["name"] for c in containers if c["health"] == "unhealthy"],
        "containers": containers,
    }


@mcp.tool(annotations=READ_ONLY)
def docker_image_versions() -> dict:
    """Image reference (image:tag), image id and registry digest of every container,
    grouped by compose project (`standalone` for containers outside compose)."""
    images: dict[str, dict] = {}
    stacks: dict[str, list] = {}
    for inspect in _inspect_all():
        image_id = inspect.get("Image")
        if image_id not in images:
            try:
                image = _docker_get(f"/images/{image_id}/json")
            except RuntimeError:
                image = {}
            images[image_id] = {
                "digests": image.get("RepoDigests") or [],
                "created": image.get("Created"),
                "version_label": ((image.get("Config") or {}).get("Labels") or {})
                .get("org.opencontainers.image.version"),
            }
        project, service = _compose((inspect.get("Config") or {}).get("Labels"))
        stacks.setdefault(project or "standalone", []).append({
            "container": (inspect.get("Name") or "").lstrip("/"),
            "service": service,
            "image": (inspect.get("Config") or {}).get("Image"),
            "image_id": image_id,
            **images[image_id],
        })
    return {name: sorted(items, key=lambda i: i["container"]) for name, items in sorted(stacks.items())}


# ── Native services ───────────────────────────────────────────────────────────

def _probe(url: str) -> dict:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # self-signed LAN services (Jeedom...)
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "cortex-ops"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT, context=context) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"up": False, "http_status": None, "error": sanitize(str(reason), 120)}
    return {
        "up": status < 500,
        "http_status": status,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


@mcp.tool(annotations=READ_ONLY)
def service_health() -> dict:
    """HTTP reachability of the native services listed in cortex-ops/config.yml
    (qBittorrent, OpenClaw, Jeedom...). Probes are anonymous: 401/403 count as up."""
    return {
        "services": [{"name": s["name"], "url": s["url"], **_probe(s["url"])}
                     for s in _config().get("services") or []],
    }


# ── Journal ───────────────────────────────────────────────────────────────────

def aggregate_journal(lines: list[str], max_groups: int) -> list[dict]:
    """Group `journalctl -o json` error entries by unit (or syslog identifier); only a
    sanitized, truncated sample of the latest message is kept per group."""
    groups: dict[str, dict] = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        source = entry.get("_SYSTEMD_UNIT") or entry.get("SYSLOG_IDENTIFIER") or "unknown"
        ts = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        message = entry.get("MESSAGE")
        if isinstance(message, list):  # binary message: byte array
            message = "<binary>"
        group = groups.setdefault(source, {"source": source, "count": 0, "first_at": ts, "last_at": ts,
                                           "priority": 7, "sample": ""})
        group["count"] += 1
        group["first_at"] = min(group["first_at"], ts)
        if ts >= group["last_at"]:
            group["last_at"] = ts
            group["sample"] = sanitize(str(message or ""))
        group["priority"] = min(group["priority"], int(entry.get("PRIORITY", 3)))
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["source"]))[:max_groups]
    for group in ranked:
        group["first_at"], group["last_at"] = _iso(group["first_at"]), _iso(group["last_at"])
    return ranked


@mcp.tool(annotations=READ_ONLY)
def journal_errors() -> dict:
    """systemd journal entries of priority err or worse over the configured window,
    aggregated per unit: count, first/last occurrence, priority and one sanitized sample
    (credentials, emails and long tokens masked, truncated)."""
    settings = _config().get("journal") or {}
    hours = int(settings.get("since_hours", 24))
    cmd = [
        "journalctl", f"--directory={HOST}/var/log/journal", "--priority=err",
        f"--since=-{hours}h", "--output=json", "--no-pager",
        "--output-fields=MESSAGE,PRIORITY,SYSLOG_IDENTIFIER,_SYSTEMD_UNIT",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"journalctl failed: {sanitize(result.stderr, 500)}")
    lines = result.stdout.splitlines()
    return {
        "since_hours": hours,
        "total": len(lines),
        "groups": aggregate_journal(lines, int(settings.get("max_groups", 30))),
    }


# ── Backups ───────────────────────────────────────────────────────────────────

def _parse_date(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@mcp.tool(annotations=READ_ONLY)
def backup_status() -> dict:
    """Last successful backup per target, from the status files the backup script writes
    in /var/lib/cortex-ops/backups/<target>.json. A target is `ok` if it succeeded within
    `max_age_hours`; expected targets without a file are reported `unknown`."""
    settings = _config().get("backups") or {}
    max_age = timedelta(hours=float(settings.get("max_age_hours", 26)))
    names = list(settings.get("targets") or [])
    names += sorted(p.stem for p in BACKUPS_DIR.glob("*.json") if p.stem not in names)
    targets = []
    for name in names:
        path = BACKUPS_DIR / f"{name}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            targets.append({"target": name, "status": "unknown", "last_success": None})
            continue
        last_success = _parse_date(data.get("last_success"))
        failed = data.get("last_result") == "failed"
        stale = last_success is None or _now() - last_success > max_age
        targets.append({
            "target": name,
            "status": "failed" if failed else "stale" if stale else "ok",
            "last_success": _iso(last_success),
            "last_attempt": _iso(_parse_date(data.get("last_attempt"))) if data.get("last_attempt") else None,
            "last_result": data.get("last_result"),
        })
    return {"max_age_hours": max_age.total_seconds() / 3600, "targets": targets}


# ── Certificates ──────────────────────────────────────────────────────────────

def _peer_certificate(endpoint: str, server_name: str) -> x509.Certificate:
    host, _, port = endpoint.partition(":")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # we read the expiry, even of an expired cert
    with socket.create_connection((host, int(port or 443)), timeout=PROBE_TIMEOUT) as sock:
        with context.wrap_socket(sock, server_hostname=server_name) as tls:
            return x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))


@mcp.tool(annotations=READ_ONLY)
def cert_expiry() -> dict:
    """Certificate served by Traefik for each host of cortex-ops/config.yml: subject,
    SANs, issuer, expiry and days left. Read from TLS handshakes, never from acme.json."""
    settings = _config().get("certificates") or {}
    endpoint = settings.get("endpoint", "traefik:443")
    results = []
    for name in settings.get("hosts") or []:
        try:
            cert = _peer_certificate(endpoint, name)
        except (OSError, ValueError) as exc:
            results.append({"host": name, "error": sanitize(str(exc), 120)})
            continue
        try:
            sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
                .get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            sans = []
        expires = cert.not_valid_after_utc
        results.append({
            "host": name,
            "subject": cert.subject.rfc4514_string(),
            "sans": sans,
            "issuer": cert.issuer.rfc4514_string(),
            "not_after": _iso(expires),
            "days_left": (expires - _now()).days,
        })
    return {"certificates": results}


# ── HTTP app ──────────────────────────────────────────────────────────────────

class BearerAuth:
    """Rejects every HTTP request without `Authorization: Bearer <CORTEX_OPS_TOKEN>`."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def create_app():
    token = os.environ.get("CORTEX_OPS_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("CORTEX_OPS_TOKEN must be set (at least 32 characters, openssl rand -hex 32)")
    allowed = [h.strip() for h in os.environ.get("CORTEX_OPS_ALLOWED_HOSTS", "172.17.0.1:*").split(",")
               if h.strip()]
    app = mcp.streamable_http_app(
        host="0.0.0.0",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed, allowed_origins=[]),
    )
    return BearerAuth(app, token)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("CORTEX_OPS_PORT", "8050")))
