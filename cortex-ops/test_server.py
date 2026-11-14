"""Unit tests of the cortex-ops parsers: `pip install -r requirements.txt pytest && pytest`."""

import json

import server

APT_HISTORY = """\
Start-Date: 2026-11-01  06:12:01
Commandline: /usr/bin/unattended-upgrade
Upgrade: libssl3t64:amd64 (3.0.13-0ubuntu3.4, 3.0.13-0ubuntu3.5), openssl:amd64 (3.0.13-0ubuntu3.4, 3.0.13-0ubuntu3.5)
End-Date: 2026-11-01  06:12:09

Start-Date: 2026-11-03  21:00:00
Commandline: apt install jq
Requested-By: clement (1000)
Install: jq:amd64 (1.7.1-3build1)
End-Date: 2026-11-03  21:00:02

Start-Date: 2026-11-05  21:00:00
Commandline: apt upgrade -y
Requested-By: clement (1000)
Upgrade: curl:amd64 (8.5.0-2ubuntu10.4, 8.5.0-2ubuntu10.5)
End-Date: 2026-11-05  21:00:05
"""


def test_parse_apt_history():
    entries = server.parse_apt_history(APT_HISTORY)
    assert entries == [
        {"date": "2026-11-01  06:12:09", "unattended": True, "upgraded": 2, "installed": 0},
        {"date": "2026-11-03  21:00:02", "unattended": False, "upgraded": 0, "installed": 1},
        {"date": "2026-11-05  21:00:05", "unattended": False, "upgraded": 1, "installed": 0},
    ]


def test_parse_apt_upgradable():
    text = (
        "Listing...\n"
        "openssl/noble-updates,noble-security 3.0.13-0ubuntu3.6 amd64 [upgradable from: 3.0.13-0ubuntu3.5]\n"
        "docker-ce/noble 5:29.1.0-1~ubuntu.24.04~noble amd64 [upgradable from: 5:29.0.2-1~ubuntu.24.04~noble]\n"
    )
    packages = server.parse_apt_upgradable(text)
    assert [(p["name"], p["security"]) for p in packages] == [("openssl", True), ("docker-ce", False)]
    assert packages[0]["current"] == "3.0.13-0ubuntu3.5"
    assert packages[0]["candidate"] == "3.0.13-0ubuntu3.6"


def test_parse_df():
    df = (
        "Filesystem                 Type       1B-blocks        Used       Avail Mounted on\n"
        "/dev/mapper/ubuntu--vg-lv  ext4    105089261568 52544630784 47166468096 /\n"
        "//192.168.1.5/data         cifs   3998634737664 3598771263898 399863473766 /mnt/synology\n"
    )
    inodes = (
        "Mounted on       Inodes   IUsed   IFree\n"
        "/               6553600  655360 5898240\n"
        "/mnt/synology         -       -       -\n"
    )
    mounts = {m["mount"]: m for m in server.parse_df(df, inodes)}
    assert mounts["/"]["used_pct"] == 52.7
    assert mounts["/"]["inodes_used_pct"] == 10.0
    assert mounts["/mnt/synology"]["fstype"] == "cifs"
    assert mounts["/mnt/synology"]["inodes_total"] is None


def test_sanitize_masks_credentials():
    text = ("login failed password=hunter2 token: abc Authorization: Bearer eyJhbGciOi.x.y "
            "user clement@example.com https://admin:s3cret@nas.local/x "
            "key 0123456789abcdef0123456789abcdef01")
    masked = server.sanitize(text, limit=500)
    for leaked in ("hunter2", "abc ", "eyJhbGciOi", "clement@example.com", "s3cret",
                   "0123456789abcdef0123456789abcdef01"):
        assert leaked not in masked
    assert server.sanitize("x" * 10 + " " * 3 + "y", limit=5) == "xxxx…"


def test_container_summary_never_exposes_env_or_labels():
    inspect = {
        "Name": "/authelia",
        "Image": "sha256:abc",
        "RestartCount": 3,
        "Config": {
            "Image": "authelia/authelia:latest",
            "Env": ["AUTHELIA_JWT_SECRET=topsecret"],
            "Labels": {"com.docker.compose.project": "cortex-internet",
                       "com.docker.compose.service": "authelia",
                       "traefik.http.middlewares.x.basicauth.users": "admin:$apr1$hash"},
        },
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "State": {"Status": "running", "Running": True, "StartedAt": "2026-11-10T08:00:00Z",
                  "Health": {"Status": "healthy"}},
    }
    summary = server.container_summary(inspect)
    assert summary["compose_project"] == "cortex-internet"
    assert summary["health"] == "healthy"
    assert summary["restart_count"] == 3
    dumped = json.dumps(summary)
    assert "topsecret" not in dumped and "apr1" not in dumped


def test_aggregate_journal():
    lines = [
        json.dumps({"_SYSTEMD_UNIT": "smbd.service", "PRIORITY": "3", "MESSAGE": "old",
                    "__REALTIME_TIMESTAMP": "1763000000000000"}),
        json.dumps({"_SYSTEMD_UNIT": "smbd.service", "PRIORITY": "2", "MESSAGE": "auth failed password=x",
                    "__REALTIME_TIMESTAMP": "1763000100000000"}),
        json.dumps({"SYSLOG_IDENTIFIER": "kernel", "PRIORITY": "3", "MESSAGE": [1, 2],
                    "__REALTIME_TIMESTAMP": "1763000050000000"}),
        "not json",
    ]
    groups = server.aggregate_journal(lines, max_groups=10)
    assert [g["source"] for g in groups] == ["smbd.service", "kernel"]
    assert groups[0]["count"] == 2 and groups[0]["priority"] == 2
    assert groups[0]["sample"] == "auth failed password=***"
    assert groups[1]["sample"] == "<binary>"
