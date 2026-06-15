"""Minimal MCP server exposing a remote Claude Code agent over HTTP.

One tool, `run_task`, runs Claude Code headless on this machine and returns
the final text answer. Synchronous and blocking — no queue, no streaming.
"""

import os
import subprocess
import sys

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

import profile_config

TIMEOUT_SECONDS = 600

# Per-agent profile, baked into the image at build time (see profile_config.py).
# Contributes no flags for the default (un-customized) image.
PROFILE = profile_config.load_profile()

# Managed platforms (Cloud Run, App Runner, Fly) inject the listen port via
# $PORT and only expose a writable filesystem under /tmp. Both default to the
# bare-metal values so nothing changes for the VM deployment.
PORT = int(os.environ.get("PORT", "8080"))
DEFAULT_CWD = os.environ.get("AGENT_DEFAULT_CWD", "/home/me/workspace")

# --- Auth: require a static bearer token, fail fast if it isn't configured ---
AGENT_TOKEN = os.environ.get("AGENT_TOKEN")
if not AGENT_TOKEN:
    sys.exit("AGENT_TOKEN env var is required (the shared bearer token)")

# StaticTokenVerifier checks `Authorization: Bearer <AGENT_TOKEN>` on every
# request and rejects anything else with 401.
auth = StaticTokenVerifier(tokens={AGENT_TOKEN: {"client_id": "sk8"}})

mcp = FastMCP("sk8", auth=auth)


@mcp.tool
def run_task(prompt: str, cwd: str = DEFAULT_CWD) -> str:
    """Delegate a complete, self-contained task to a remote agent on another machine.

    The remote agent is a full Claude Code instance running headless. It does NOT
    share your conversation, files, or context — so the `prompt` must carry
    everything it needs: the goal, relevant background, constraints, and the exact
    deliverable you expect. Write it as a standalone brief, not a follow-up.

    The call is synchronous and blocking: it runs the task to completion (up to
    600s) and returns only the agent's final text answer — there is no streaming,
    progress, or status to poll. Use `cwd` to point the agent at the directory it
    should work in on the remote machine.

    Returns the agent's final answer, or a string starting with "AGENT_ERROR:" on
    failure.
    """
    try:
        # The cwd may not exist yet — the default scratch dir ($AGENT_DEFAULT_CWD,
        # e.g. /tmp/workspace) lives on an in-memory FS that starts empty on every
        # managed-platform instance, so nothing creates it ahead of time.
        os.makedirs(cwd, exist_ok=True)
        # Profile flags (allowed/disallowed tools, appended system prompt,
        # --mcp-config) come from the baked-in profile; empty list by default.
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text",
             "--permission-mode", "bypassPermissions",
             *profile_config.to_cli_args(PROFILE)],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            # No channel to approve prompts mid-run, so the agent must run fully
            # unattended. bypassPermissions refuses to run as root unless
            # IS_SANDBOX marks the host (a container) as sandboxed.
            env={**os.environ, "IS_SANDBOX": "1"},
        )
    except subprocess.TimeoutExpired:
        return f"AGENT_ERROR: timed out after {TIMEOUT_SECONDS}s"
    except Exception as exc:  # e.g. cwd missing, claude not on PATH
        return f"AGENT_ERROR: {exc}"

    if result.returncode != 0:
        return f"AGENT_ERROR: {result.stderr.strip()}"
    return result.stdout


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=PORT)
