"""sk8 MCP server — Claude Agent SDK backend (prototype).

Same one-tool MCP surface as `server.py`, but `run_task` drives the agent
through the **Claude Agent SDK** (`query()`) instead of shelling out to
`claude -p` and parsing stdout.

What this buys over the subprocess version:
  * a typed, async message stream instead of a flat text blob — we can see
    each assistant turn (and could surface tool calls / progress);
  * structured permission control via `permission_mode` rather than relying on
    CLI flags;
  * a `ResultMessage` carrying the final text plus cost / duration / usage.

What it does NOT change: the SDK still spawns the `claude` binary under the
hood, so the box still needs Claude Code installed and authenticated, and the
"runs as root → needs IS_SANDBOX=1 with bypassPermissions" caveat from the
systemd unit still applies. This is a richer interface over the same engine,
not a different engine.

Install the extra dependency alongside fastmcp:
    uv pip install claude-agent-sdk      # or: pip install claude-agent-sdk
"""

import asyncio
import os
import sys

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

import profile_config

TIMEOUT_SECONDS = 600

# Per-agent profile, baked into the image at build time (see profile_config.py).
# {} for the default (un-customized) image, so the splat below is a no-op then.
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


async def _run(prompt: str, cwd: str) -> str:
    """Drive one task to completion via the Agent SDK, return the final text."""
    # The cwd may not exist yet — the default scratch dir ($AGENT_DEFAULT_CWD,
    # e.g. /tmp/workspace) lives on an in-memory FS that starts empty on every
    # managed-platform instance, so nothing creates it ahead of time.
    os.makedirs(cwd, exist_ok=True)
    opts = dict(
        cwd=cwd,
        # Run unattended: there is no human to approve tool calls mid-task, so
        # the agent must not block on a permission prompt. This is the SDK
        # equivalent of `--permission-mode bypassPermissions` in server.py and
        # carries the same blast radius — see the security notes in the README.
        permission_mode="bypassPermissions",
        # Behave like Claude Code (its full toolset + system prompt), so this is
        # a true drop-in for the `claude -p` backend rather than a bare model.
        system_prompt={"type": "preset", "preset": "claude_code"},
        # bypassPermissions refuses to run as root unless IS_SANDBOX marks the
        # host (a container) as sandboxed. Inject it into the spawned `claude`'s
        # env, mirroring server.py.
        env={**os.environ, "IS_SANDBOX": "1"},
    )
    # Layer per-agent profile customization on top, native to the SDK:
    # allowed_tools, disallowed_tools, mcp_servers, and a system_prompt that
    # appends the profile persona onto the Claude Code preset above. Empty for
    # the default image, so this overrides nothing there.
    opts.update(profile_config.to_sdk_kwargs(PROFILE))
    options = ClaudeAgentOptions(**opts)

    final = None          # ResultMessage.result, if the SDK gives us one
    transcript: list[str] = []  # fallback: every assistant text block, joined

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    transcript.append(block.text)
        elif isinstance(message, ResultMessage):
            if message.is_error:
                return f"AGENT_ERROR: {message.subtype}"
            final = message.result

    return final if final is not None else "\n".join(transcript)


@mcp.tool
async def run_task(prompt: str, cwd: str = DEFAULT_CWD) -> str:
    """Delegate a complete, self-contained task to a remote agent on another machine.

    The remote agent is a full Claude Code instance driven via the Claude Agent
    SDK. It does NOT share your conversation, files, or context — so the `prompt`
    must carry everything it needs: the goal, relevant background, constraints,
    and the exact deliverable you expect. Write it as a standalone brief, not a
    follow-up.

    The call is synchronous and blocking: it runs the task to completion (up to
    600s) and returns only the agent's final text answer. Use `cwd` to point the
    agent at the directory it should work in on the remote machine.

    Returns the agent's final answer, or a string starting with "AGENT_ERROR:" on
    failure.
    """
    try:
        return await asyncio.wait_for(_run(prompt, cwd), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return f"AGENT_ERROR: timed out after {TIMEOUT_SECONDS}s"
    except Exception as exc:  # e.g. cwd missing, claude not on PATH, SDK error
        return f"AGENT_ERROR: {exc}"


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=PORT)
