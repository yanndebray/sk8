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

import file_io
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


async def _run(prompt: str, cwd: str, inputs: list[str] | None = None,
               files: list[dict] | None = None) -> str:
    """Drive one task to completion via the Agent SDK, return the final text."""
    # The cwd may not exist yet — the default scratch dir ($AGENT_DEFAULT_CWD,
    # e.g. /tmp/workspace) lives on an in-memory FS that starts empty on every
    # managed-platform instance, so nothing creates it ahead of time.
    os.makedirs(cwd, exist_ok=True)
    # Clear any leftover inputs/outputs from a prior run on this (possibly warm)
    # instance so stale files aren't reused or re-returned. Unconditional:
    # inline transfer (no bucket) uses the same dirs, so don't gate on GCS.
    file_io.reset_workspace(cwd)
    # google-cloud-storage is blocking, so run it off the event loop.
    prompt = await asyncio.to_thread(_prepare_inputs, prompt, cwd, inputs, files)
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
        # task_env() sets IS_SANDBOX (bypassPermissions refuses to run as root
        # otherwise) and puts the profile venv first on PATH so the agent's
        # python3 sees the profile's packages (not uv's project venv). Mirrors
        # server.py.
        env=profile_config.task_env(),
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

    text = final if final is not None else "\n".join(transcript)
    return text + await asyncio.to_thread(_collect_outputs, cwd)


def _prepare_inputs(prompt: str, cwd: str, inputs: list[str] | None,
                    files: list[dict] | None) -> str:
    """Materialize inputs into cwd/inputs/ and prepend a note about them.

    Two sources share one dir: `files` are small inline (base64) uploads needing
    no bucket; `inputs` are GCS object keys (requires file transfer enabled).
    Names are deduped across both so a shared basename never clobbers.
    """
    if not inputs and not files:
        return prompt
    used: set[str] = set()
    local: list[str] = []
    if files:
        local += file_io.write_inline_inputs(files, cwd, used)
    if inputs:
        if not file_io.enabled():
            raise file_io.FileIOError(
                "GCS inputs require file transfer (GCS_BUCKET unset); pass small "
                "files inline via `files` instead.")
        local += file_io.download_inputs(inputs, cwd, used)
    listing = "\n".join(f"- {p}" for p in local)
    note = (
        "Input files have been downloaded for this task:\n"
        f"{listing}\n"
        "Write any deliverables you want returned to the caller into the "
        "'outputs/' subdirectory of the working directory.\n\n"
    )
    return note + prompt


def _collect_outputs(cwd: str) -> str:
    """Render cwd/outputs/* as a trailing Artifacts block (empty if none).

    GCS-backed agents upload and return signed URLs; otherwise small files come
    back inline as base64 (over-cap files are listed, not returned).
    """
    if file_io.enabled():
        return file_io.format_artifacts(file_io.upload_outputs(cwd))
    return file_io.format_inline_artifacts(*file_io.inline_outputs(cwd))


@mcp.tool
async def run_task(prompt: str, cwd: str = DEFAULT_CWD,
                   inputs: list[str] | None = None,
                   files: list[dict] | None = None) -> str:
    """Delegate a complete, self-contained task to a remote agent on another machine.

    The remote agent is a full Claude Code instance driven via the Claude Agent
    SDK. It does NOT share your conversation, files, or context — so the `prompt`
    must carry everything it needs: the goal, relevant background, constraints,
    and the exact deliverable you expect. Write it as a standalone brief, not a
    follow-up.

    The call is synchronous and blocking: it runs the task to completion (up to
    600s) and returns only the agent's final text answer. Use `cwd` to point the
    agent at the directory it should work in on the remote machine.

    File transfer — two ways to send files in, both landing in `cwd/inputs/`:
      * `files` (small files, no bucket needed): a list of
        `{"name": str, "content_base64": str}`. Each is capped at ~8 MB; larger
        ones are rejected with a pointer to the signed-URL path.
      * `inputs` (large files, GCS-backed agents only): a list of GCS object keys
        from `request_upload_url`, downloaded before the run.
    Anything the agent writes under `cwd/outputs/` comes back in a trailing
    "Artifacts:" block: signed download URLs when the agent is GCS-backed, else
    small files inline as base64 (over-cap files are listed, not returned). Use
    this instead of embedding file bytes in the prompt.

    Returns the agent's final answer, or a string starting with "AGENT_ERROR:" on
    failure.
    """
    try:
        return await asyncio.wait_for(
            _run(prompt, cwd, inputs, files), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return f"AGENT_ERROR: timed out after {TIMEOUT_SECONDS}s"
    except Exception as exc:  # e.g. cwd missing, claude not on PATH, SDK/GCS error
        return f"AGENT_ERROR: {exc}"


# File-transfer tools only exist when the agent is GCS-backed (GCS_BUCKET set).
# On a default / local image they are never registered, so the surface is
# identical to before. GCS calls are blocking, so run them off the event loop.
if file_io.enabled():
    @mcp.tool
    async def request_upload_url(filename: str,
                                 content_type: str = "application/octet-stream") -> dict:
        """Mint a signed PUT URL to upload one input file directly to GCS.

        Uploading straight to the bucket bypasses MCP's JSON-RPC envelope and
        Cloud Run's 32 MB request cap. PUT the bytes to `upload_url` (with the
        matching Content-Type), then pass the returned `object` key to
        `run_task(inputs=[...])`.
        """
        return await asyncio.to_thread(
            file_io.request_upload_url, filename, content_type)

    @mcp.tool
    async def fetch_result(object: str) -> dict:
        """Mint a fresh signed GET URL for an artifact object (signed URLs expire).

        Use it to re-download an object listed in a previous run_task
        "Artifacts:" section straight from GCS.
        """
        return await asyncio.to_thread(file_io.fetch_result, object)


if __name__ == "__main__":
    # stateless_http: Cloud Run is multi-instance + scale-to-zero, so per-instance
    # MCP session state breaks (a session id minted on one instance 404s on the
    # next). Stateless mode avoids issuing one. See issue #14 Phase 0.
    mcp.run(transport="http", host="0.0.0.0", port=PORT, stateless_http=True)
