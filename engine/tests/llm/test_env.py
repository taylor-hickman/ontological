"""The spawned `claude`'s environment (ARCHITECTURE.md §3 rule 3, §6.2; spike S1).

`options.env` is merged over os.environ and can't remove a key, so the engine deletes the Claude
Code session variables from its own environment at startup, and every call blanks the four auth
variables, which `claude` treats as unset. A stray API key would bill the user's API account.
"""

from __future__ import annotations

from ontological.engine import main
from ontological.llm.env import AUTH_VARIABLES, SESSION_VARIABLES, call_env, sanitize_environ

# The session variables spike S1 found Claude Code 2.1.318 setting for its children.
S1_SESSION_VARIABLES = {
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
}
AUTH = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"}


def test_the_lists_are_s1s() -> None:
    assert set(SESSION_VARIABLES) == S1_SESSION_VARIABLES
    assert set(AUTH_VARIABLES) == AUTH


def test_sanitize_environ_drops_the_session_variables_and_keeps_the_rest() -> None:
    # The auth variables stay, so onboarding can say once that Ontological ignores them (§3);
    # every call blanks them instead.
    kept = {"PATH": "/usr/bin", "HOME": "/Users/me", "CLAUDE_CONFIG_DIR": "/Users/me/.c"}
    kept |= dict.fromkeys(AUTH, "x")
    environ = dict.fromkeys(S1_SESSION_VARIABLES, "x") | kept
    sanitize_environ(environ)
    assert environ == kept


def test_the_engine_sanitizes_with_the_gateways_lists() -> None:
    assert main.sanitize_environ is sanitize_environ


def test_every_call_blanks_the_auth_variables() -> None:
    assert call_env() == dict.fromkeys(AUTH, "")
