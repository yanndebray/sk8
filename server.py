"""Minimal MCP server exposing a remote Claude Code agent over HTTP.

One tool, `run_task`, runs Claude Code headless on this machine and returns
the final text answer. Synchronous and blocking — no queue, no streaming.
"""

import os
import subprocess
import sys

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

import file_io
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
def run_task(prompt: str, cwd: str = DEFAULT_CWD,
             inputs: list[str] | None = None,
             files: list[dict] | None = None) -> str:
    """Delegate a complete, self-contained task to a remote agent on another machine.

    The remote agent is a full Claude Code instance running headless. It does NOT
    share your conversation, files, or context — so the `prompt` must carry
    everything it needs: the goal, relevant background, constraints, and the exact
    deliverable you expect. Write it as a standalone brief, not a follow-up.

    The call is synchronous and blocking: it runs the task to completion (up to
    600s) and returns only the agent's final text answer — there is no streaming,
    progress, or status to poll. Use `cwd` to point the agent at the directory it
    should work in on the remote machine.

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
        # The cwd may not exist yet — the default scratch dir ($AGENT_DEFAULT_CWD,
        # e.g. /tmp/workspace) lives on an in-memory FS that starts empty on every
        # managed-platform instance, so nothing creates it ahead of time.
        os.makedirs(cwd, exist_ok=True)
        # Clear any leftover inputs/outputs from a prior run on this (possibly
        # warm) instance so stale files aren't reused or re-returned. Cheap no-op
        # when the dirs don't exist. Unconditional: inline transfer (no bucket)
        # populates the same dirs, so this can't be gated on GCS being enabled.
        file_io.reset_workspace(cwd)
        prompt = _prepare_inputs(prompt, cwd, inputs, files)
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
            # unattended. task_env() sets IS_SANDBOX (bypassPermissions refuses
            # to run as root otherwise) and puts the profile venv first on PATH
            # so the agent's python3 sees the profile's packages.
            env=profile_config.task_env(),
        )
    except subprocess.TimeoutExpired:
        return f"AGENT_ERROR: timed out after {TIMEOUT_SECONDS}s"
    except Exception as exc:  # e.g. cwd missing, claude not on PATH, GCS failure
        return f"AGENT_ERROR: {exc}"

    if result.returncode != 0:
        return f"AGENT_ERROR: {result.stderr.strip()}"
    # Output upload can fail (GCS/signing); keep it inside the AGENT_ERROR
    # convention rather than letting it escape as a raw tool exception.
    try:
        return result.stdout + _collect_outputs(cwd)
    except Exception as exc:
        return f"AGENT_ERROR: {exc}"


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


# File-transfer tools only exist when the agent is GCS-backed (GCS_BUCKET set).
# On a default / local image they are never registered, so the surface is
# identical to before.
if file_io.enabled():
    @mcp.tool
    def request_upload_url(filename: str,
                           content_type: str = "application/octet-stream") -> dict:
        """Mint a signed PUT URL to upload one input file directly to GCS.

        Uploading straight to the bucket bypasses MCP's JSON-RPC envelope and
        Cloud Run's 32 MB request cap. PUT the bytes to `upload_url` (with the
        matching Content-Type), then pass the returned `object` key to
        `run_task(inputs=[...])`.
        """
        return file_io.request_upload_url(filename, content_type)

    @mcp.tool
    def fetch_result(object: str) -> dict:
        """Mint a fresh signed GET URL for an artifact object (signed URLs expire).

        Use it to re-download an object listed in a previous run_task
        "Artifacts:" section straight from GCS.
        """
        return file_io.fetch_result(object)


if __name__ == "__main__":
    # stateless_http: Cloud Run is multi-instance + scale-to-zero, so per-instance
    # MCP session state breaks (a session id minted on one instance 404s on the
    # next). Stateless mode avoids issuing one. See issue #14 Phase 0.
    mcp.run(transport="http", host="0.0.0.0", port=PORT, stateless_http=True)
