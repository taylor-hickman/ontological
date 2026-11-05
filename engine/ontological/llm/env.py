"""The spawned `claude`'s environment (ARCHITECTURE.md §3 rule 3, §6.2; spike S1).

The SDK merges `options.env` over `os.environ`, so a call can override a variable but can't
remove one. Two measures follow:

- The engine deletes the session variables a Claude Code parent sets from its own environment at
  startup, before anything spawns `claude`, so a spawned `claude` doesn't act as a nested
  session. An engine started by the MCP shim inherits them.
- Every call sets the auth variables to "", which `claude` treats as unset. In `-p` mode an API
  key takes precedence over the user's login and would bill API credits. They stay in the
  engine's own environment so onboarding can say once that Ontological ignores them.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

AUTH_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

# What spike S1 found Claude Code 2.1.318 setting for its children.
SESSION_VARIABLES = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_AUTO_CONNECT_IDE",
    "CLAUDE_CODE_ENABLE_TODO_TOOLS",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)


def sanitize_environ(environ: MutableMapping[str, str] = os.environ) -> None:
    """Delete the Claude Code session variables; the engine runs this at startup."""
    for name in SESSION_VARIABLES:
        environ.pop(name, None)


def call_env() -> dict[str, str]:
    """`options.env` for every call: the auth variables, blanked."""
    return dict.fromkeys(AUTH_VARIABLES, "")
