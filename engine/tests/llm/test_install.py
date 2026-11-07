"""Detecting the user's Claude Code for onboarding (ARCHITECTURE.md §3, §6.5): version, login and
the ignored auth variables, against a fake `claude` script."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from ontological.llm.gateway import install_locations, resolve_cli
from ontological.llm.install import ClaudeInstall, detect, ignored_variables


def fake_claude(
    tmp_path: Path,
    *,
    version: str = "2.1.319 (Claude Code)",
    auth: str = '{"loggedIn": true, "authMethod": "claude.ai", "email": "x@example.com"}',
    auth_exit: int = 0,
) -> Path:
    """A `claude` that prints `version` for --version and `auth` for `auth status --json`. It
    writes the ANTHROPIC_API_KEY it saw to seen-key, so a test can check it was blanked."""
    path = tmp_path / "claude"
    seen = tmp_path / "seen-key"
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo \'{version}\'; exit 0; fi\n'
        'if [ "$1 $2 $3" = "auth status --json" ]; then\n'
        f'  printf "%s" "$ANTHROPIC_API_KEY" > "{seen}"\n'
        f"  echo '{auth}'; exit {auth_exit}\n"
        "fi\n"
        "exit 2\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def run(cli: Path | None, environ: dict[str, str] | None = None) -> ClaudeInstall:
    return asyncio.run(detect(cli, environ=environ or {}))


def test_a_logged_in_claude_is_ready(tmp_path: Path) -> None:
    got = run(fake_claude(tmp_path))
    assert (got.state, got.version, got.logged_in) == ("ready", "2.1.319", True)
    assert got.min_version == "2.1.318"


def test_a_missing_claude_is_not_installed(tmp_path: Path) -> None:
    got = run(tmp_path / "nowhere")
    assert (got.state, got.cli_path, got.version, got.logged_in) == (
        "not_installed",
        None,
        None,
        None,
    )


def test_an_old_claude_is_outdated(tmp_path: Path) -> None:
    got = run(fake_claude(tmp_path, version="2.1.200 (Claude Code)"))
    assert (got.state, got.version) == ("outdated", "2.1.200")


def test_logged_out_needs_login_whatever_the_exit_code(tmp_path: Path) -> None:
    got = run(fake_claude(tmp_path, auth='{"loggedIn": false}', auth_exit=1))
    assert (got.state, got.logged_in) == ("needs_login", False)


@pytest.mark.parametrize("auth", ["not json", '{"other": 1}', '{"loggedIn": "yes"}', "[]"])
def test_an_unreadable_auth_status_is_unknown(tmp_path: Path, auth: str) -> None:
    got = run(fake_claude(tmp_path, auth=auth))
    assert (got.state, got.logged_in) == ("unknown", None)


def test_auth_status_runs_with_the_api_key_blanked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-seen")
    run(fake_claude(tmp_path))
    assert (tmp_path / "seen-key").read_text() == ""


def test_set_auth_variables_are_named_as_ignored(tmp_path: Path) -> None:
    environ = {
        "ANTHROPIC_API_KEY": "sk-x",
        "ANTHROPIC_BASE_URL": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "t",
    }
    assert ignored_variables(environ) == ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]
    assert run(fake_claude(tmp_path), environ).ignored_variables == [
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ]


def test_without_claude_on_path_the_native_install_folder_is_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An app opened from Finder: launchd's PATH, with no ~/.local/bin on it.
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    home = tmp_path / "home"
    native = fake_claude_at(home / ".local" / "bin")
    assert resolve_cli(None, home=home) == native


def test_claude_on_path_wins_over_the_install_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = fake_claude_at(tmp_path / "bin")
    fake_claude_at(tmp_path / "home" / ".local" / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert resolve_cli(None, home=tmp_path / "home") == on_path


def test_the_install_folders_are_where_the_installers_put_claude(tmp_path: Path) -> None:
    assert install_locations(tmp_path)[:2] == [
        tmp_path / ".local" / "bin" / "claude",
        tmp_path / ".claude" / "local" / "claude",
    ]


def fake_claude_at(folder: Path) -> Path:
    folder.mkdir(parents=True)
    return fake_claude(folder)
