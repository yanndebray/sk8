"""Minimal MCP server exposing a single remote-agent tool over HTTP.

Runs Claude Code headless on this machine and returns the final answer.
"""
import os
import subprocess
import sys

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

# --- Auth: require a static bearer token, fail fast if unset ---------------
AGENT_TOKEN = os.environ.get("AGENT_TOKEN")
if not AGENT_TOKEN:
    sys.exit("AGENT_TOKEN environment variable is not set; refusing to start.")

# StaticTokenVerifier maps an accepted bearer token to its claims. Any request
# without `Authorization: Bearer <AGENT_TOKEN>` is rejected by FastMCP.
auth = StaticTokenVerifier(tokens={AGENT_TOKEN: {"client_id": "remote-agent-client"}})

mcp = FastMCP("remote-agent", auth=auth)

TIMEOUT_SECONDS = 600


@mcp.tool
def run_task(prompt: str, cwd: str = "/root/workspace") -> str:
    """Delegate a complete, self-contained task to a remote autonomous agent.

    This hands the entire task off to a Claude Code agent running on a remote
    machine. The remote agent does NOT share your conversation, files, or
    context — so the `prompt` must be fully self-contained: state the goal,
    all relevant background, constraints, and exactly what output you expect.
    You get back ONLY the agent's final text answer (no streaming, no
    intermediate steps, no follow-up). The call blocks until the agent
    finishes, so prefer one well-scoped task per call.

    Args:
        prompt: The complete, standalone instruction for the remote agent.
        cwd: Working directory on the remote machine to run the task in.

    Returns:
        The agent's final answer on success, or a string beginning with
        "AGENT_ERROR:" if the run failed or timed out.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text",
             "--permission-mode", "bypassPermissions"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            # The call is blocking with no channel to approve prompts mid-run,
            # so the agent must run fully unattended. bypassPermissions refuses
            # to run as root unless IS_SANDBOX marks this as a sandboxed host.
            env={**os.environ, "IS_SANDBOX": "1"},
        )
    except subprocess.TimeoutExpired:
        return f"AGENT_ERROR: timed out after {TIMEOUT_SECONDS}s"
    except Exception as exc:  # e.g. cwd missing, claude not found
        return f"AGENT_ERROR: {exc}"

    if result.returncode != 0:
        return f"AGENT_ERROR: {result.stderr.strip()}"
    return result.stdout


if __name__ == "__main__":
    # Bind to localhost only: Caddy reverse-proxies to it over the loopback,
    # so :8080 is never exposed publicly (clients reach it via HTTPS/Caddy).
    mcp.run(transport="http", host="127.0.0.1", port=8080)