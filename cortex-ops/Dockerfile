# Same release as the host: apt reads the host's dpkg status and lists, and journalctl
# its journal files, so both should match Cortex's versions (Ubuntu 24.04).
FROM ubuntu:24.04

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv systemd ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN python3 -m venv /opt/venv
COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Empty legacy sources.list: only the host's sources.list.d (deb822) is used.
RUN touch /app/empty.list
COPY server.py .

EXPOSE 8050
CMD ["/opt/venv/bin/python", "server.py"]
