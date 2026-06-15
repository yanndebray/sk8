"""Per-agent profile loader (shared by both server backends).

A *profile* customizes what one agent can do: which tools it may use, which MCP
servers it connects to, and an optional system-prompt persona. It is baked into
the image at build time (see Dockerfile / cloudbuild.yaml) and lands at
``$AGENT_PROFILE_DIR`` (default ``/profile``):

    /profile/
      profile.json     # {allowed_tools, disallowed_tools, system_prompt, strict_mcp_config}
      mcp.json         # optional --mcp-config payload: {"mcpServers": {...}}
      requirements.txt # installed into the agent's venv at build time (not read here)
      skills/          # copied into ~/.claude/skills at build time (not read here)

Everything is optional. An absent / empty ``profile.json`` means "behave exactly
as the un-customized default" — this module then contributes no flags or options.

Stdlib-only (json + os) so it imports cleanly under either backend.
"""

from __future__ import annotations

import json
import os

PROFILE_DIR = os.environ.get("AGENT_PROFILE_DIR", "/profile")


def load_profile() -> dict:
    """Return the parsed profile.json, or {} if absent/unreadable/malformed."""
    path = os.path.join(PROFILE_DIR, "profile.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def mcp_config_path() -> str | None:
    """Path to the profile's mcp.json (the --mcp-config payload), or None."""
    path = os.path.join(PROFILE_DIR, "mcp.json")
    return path if os.path.isfile(path) else None


def to_cli_args(profile: dict) -> list[str]:
    """Map a profile to extra `claude -p` flags (CLI backend, server.py)."""
    args: list[str] = []
    allowed = profile.get("allowed_tools")
    if allowed:
        args += ["--allowed-tools", *allowed]
    disallowed = profile.get("disallowed_tools")
    if disallowed:
        args += ["--disallowed-tools", *disallowed]
    system_prompt = profile.get("system_prompt")
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    mcp_path = mcp_config_path()
    if mcp_path:
        args += ["--mcp-config", mcp_path]
        if profile.get("strict_mcp_config"):
            args += ["--strict-mcp-config"]
    return args


def to_sdk_kwargs(profile: dict) -> dict:
    """Map a profile to ClaudeAgentOptions kwargs (SDK backend, server_sdk.py).

    Returns only the keys the profile actually sets, so the caller can splat it
    over the base options without overriding unrelated defaults. The base server
    keeps the Claude Code preset system prompt; a profile `system_prompt` is
    *appended* to it (persona on top of Claude Code), not a replacement.
    """
    kwargs: dict = {}
    allowed = profile.get("allowed_tools")
    if allowed:
        kwargs["allowed_tools"] = allowed
    disallowed = profile.get("disallowed_tools")
    if disallowed:
        kwargs["disallowed_tools"] = disallowed
    system_prompt = profile.get("system_prompt")
    if system_prompt:
        kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        }
    mcp_path = mcp_config_path()
    if mcp_path:
        try:
            with open(mcp_path, encoding="utf-8") as fh:
                servers = json.load(fh).get("mcpServers", {})
            if servers:
                kwargs["mcp_servers"] = servers
        except (ValueError, OSError):
            pass
    return kwargs
