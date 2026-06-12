# agent-as-MCP

A minimal [FastMCP](https://gofastmcp.com) server that exposes **one tool**,
`run_task`, over HTTP. The tool delegates a complete, self-contained task to a
Claude Code agent running headless on *this* machine and returns the agent's
final text answer. Synchronous and blocking — no queue, no streaming, no status
polling.

## How it works

`run_task(prompt, cwd)` shells out to:

```
claude -p <prompt> --output-format text --permission-mode bypassPermissions
```

run inside `cwd`, with a 600 s timeout. On success you get stdout; on failure
or timeout you get a string starting with `AGENT_ERROR:`.

Requires the `claude` CLI to be installed and authenticated on this machine.

**Why `bypassPermissions`:** the call is a single blocking request that returns
only the agent's final text — there is no channel to approve tool prompts
mid-run. So the spawned agent must run fully unattended; otherwise any task
needing Bash/Write stalls forever on an approval it can never receive.
`bypassPermissions` refuses to run under root unless the host is marked a
sandbox, so the server also sets `IS_SANDBOX=1` in the subprocess environment.
This means the remote agent runs **every tool with no confirmation** — see
Limitations.

## Architecture (as deployed)

```
laptop ──HTTPS──> Caddy :443 ──HTTP──> 127.0.0.1:8080 (FastMCP) ──> claude CLI
        (Let's Encrypt)      (loopback only)
```

- **FastMCP server** (`server.py`) binds **`127.0.0.1:8080`** — never exposed
  publicly. The only way in is via Caddy.
- **Caddy** terminates TLS on `:443` for the public domain and reverse-proxies
  to the loopback server, preserving the path (`/mcp` → `localhost:8080/mcp`).
- Both run as **systemd services** so they survive reboots and restart on
  failure.

This repo's example domain is `DOMAIN_EXAMPLE`; substitute your
own throughout.

## Token

`AGENT_TOKEN` gates every request (`Authorization: Bearer <token>`); the server
refuses to start if it is unset. Keep it in a `.env` file (git-ignored) so the
systemd unit and your shells share one persistent value:

```bash
echo "AGENT_TOKEN=$(openssl rand -hex 16)" > .env
chmod 600 .env
```

The exact string in `.env` is what your laptop registers with — see below.

## Setup

### 1. Dependencies

```bash
uv sync
```

### 2. Run the MCP server as a systemd service

`/etc/systemd/system/remote-agent-mcp.service`:

```ini
[Unit]
Description=Remote Agent MCP server (FastMCP -> claude CLI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/remote-agent-mcp
EnvironmentFile=/root/remote-agent-mcp/.env
# The default systemd PATH omits ~/.local/bin (where the claude CLI lives) and
# sets no HOME, so claude can't be found or locate its ~/.claude credentials.
# Both are required for run_task to spawn the agent.
Environment=HOME=/root
Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/root/.local/bin/uv run server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now remote-agent-mcp
systemctl is-active remote-agent-mcp        # -> active
ss -ltnp | grep 127.0.0.1:8080              # listening on loopback
```

(For quick local hacking without systemd you can still run it by hand:
`set -a; source .env; set +a; uv run server.py`.)

### 3. Caddy reverse proxy with automatic HTTPS

Install Caddy (Debian/Ubuntu, official apt repo):

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

DNS prerequisite for automatic HTTPS: the domain's **A (and AAAA, if any)
record must point directly at this box's public IP** — *not* proxied through a
CDN such as Cloudflare. If it sits behind Cloudflare's proxy (orange cloud),
set the record to **DNS-only (grey cloud)**, otherwise Caddy's Let's Encrypt
challenge can't complete. Verify with `dig +short A your.domain @1.1.1.1`.

`/etc/caddy/Caddyfile`:

```caddy
DOMAIN_EXAMPLE {
	# Reverse-proxy everything (including /mcp) to the local FastMCP server.
	# Path is preserved, so /mcp -> 127.0.0.1:8080/mcp.
	# flush_interval -1 disables response buffering so MCP's Server-Sent
	# Events (text/event-stream) stream through immediately.
	# Use 127.0.0.1 (not "localhost") to match the server's IPv4 bind —
	# "localhost" can resolve to [::1] and fail to connect.
	reverse_proxy 127.0.0.1:8080 {
		flush_interval -1
	}
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
journalctl -u caddy -n 30 | grep -i 'certificate obtained'   # cert issued
```

Caddy fetches and auto-renews the Let's Encrypt certificate; no further action
needed.

## Smoke test (curl)

MCP's streamable-HTTP transport is session-based: you `initialize` to get a
session id, send the `initialized` notification, then call `tools/list`. This
works against either the local backend (`http://127.0.0.1:8080/mcp`) or the
public endpoint (`https://DOMAIN_EXAMPLE/mcp`):

```bash
URL=https://DOMAIN_EXAMPLE/mcp
TOKEN=$(cut -d= -f2 .env)

# 1. initialize — grab the mcp-session-id response header
curl -s -D /tmp/hdrs.txt "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'

SID=$(grep -i '^mcp-session-id:' /tmp/hdrs.txt | awk '{print $2}' | tr -d '\r')

# 2. notifications/initialized
curl -s -o /dev/null "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# 3. tools/list — should list run_task
curl -s "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

The final response includes `"name":"run_task"`. A request with a missing or
wrong bearer token returns `401`.

## Register from your laptop

```bash
claude mcp add --transport http remote-agent \
  https://DOMAIN_EXAMPLE/mcp \
  --header "Authorization: Bearer <AGENT_TOKEN>"
```

Use the HTTPS URL and the `<AGENT_TOKEN>` value from the server's `.env`.

## Verify

```bash
claude mcp list        # remote-agent should show as connected
```

Then, in a Claude Code session on your laptop:

> Use the remote-agent run_task tool with prompt: "list the files in the
> current directory and summarize them".

## Operations

```bash
systemctl status remote-agent-mcp caddy   # health of both services
journalctl -u remote-agent-mcp -f         # MCP server logs
journalctl -u caddy -f                    # proxy / access logs
sudo systemctl restart remote-agent-mcp   # after editing server.py or .env
sudo systemctl reload caddy               # after editing the Caddyfile
```

## Limitations

- **Synchronous only.** Each call blocks until the remote agent finishes (up to
  the 600 s timeout). No streaming, no job ids, no progress.
- **One task at a time** in practice — a long task ties up the call. There is no
  queue or concurrency management.
- **Fully autonomous agent.** The spawned `claude` runs in
  `bypassPermissions` mode (with `IS_SANDBOX=1` to allow it as root): it
  executes every tool — Bash, Write, network — with **no confirmation**. A
  single static bearer token is the *only* thing gating that. Anyone with the
  token can run arbitrary commands as root on this box, in whatever `cwd` they
  pass. Treat the token as a production secret, scope `cwd` deliberately, and
  only run this on a host you're willing to hand over completely. For tighter
  scoping, replace `--permission-mode bypassPermissions` in `server.py` with
  `--allowedTools "Bash Write Edit Read"` (grants only those tools; tasks
  needing anything else will stall).
- **Public exposure.** The server is reachable over the internet via the domain.
  Consider a host firewall allowing only `:443`/`:22`. The loopback bind means
  `:8080` itself is not directly reachable; all traffic must pass through Caddy
  (TLS + token). For a private alternative, skip the public DNS/Caddy entirely
  and use an SSH tunnel: `ssh -L 8080:localhost:8080 host`.