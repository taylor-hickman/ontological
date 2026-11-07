"""Bridge SQL (BUILD_PLAN.md M3.5): upstream's prompt on the `bridge_sql` route, and Claude
picking the owning Term where upstream reranked. The gateway is scripted."""

from __future__ import annotations

import asyncio
from typing import Any

from fakes import ScriptedGateway

from ontological.compile import bridges
from ontological.compile.models import OwnedBridgeSqlAttributeProposal
from ontological.llm.routes import DEFAULT_ROUTES, Route

PLAYLIST_TRACK: dict[str, Any] = {
    "table_id": "t-pt",
    "schema_name": "main",
    "table_name": "PlaylistTrack",
    "fk_pairs": [
        {"source_column": "PlaylistId", "target_schema": "main", "target_table": "Playlist",
         "target_column": "PlaylistId", "target_table_id": "t-p"},
        {"source_column": "TrackId", "target_schema": "main", "target_table": "Track",
         "target_column": "TrackId", "target_table_id": "t-t"},
    ],
}  # fmt: skip
PLAYLIST = {"id": "term-p", "name": "Playlist", "description": "A named list of tracks."}
TRACK = {"id": "term-t", "name": "Track", "description": ""}


def proposal(owner: str | None = None) -> dict[str, Any]:
    out = {
        "name": "Playlist Tracks",
        "description": "Which tracks each playlist holds.",
        "expression": "SELECT * FROM main.PlaylistTrack",
    }
    return out if owner is None else {**out, "owner_term": owner}


def test_bridge_sql_runs_on_opus() -> None:
    assert DEFAULT_ROUTES["bridge_sql"] == Route("opus", "high")


def test_one_candidate_owns_it_with_upstream_s_prompt_unchanged() -> None:
    gateway = ScriptedGateway(lambda task, user: proposal())

    got = asyncio.run(bridges.propose(gateway, PLAYLIST_TRACK, [PLAYLIST]))

    assert got is not None and got[1] == PLAYLIST
    [call] = gateway.calls
    assert (call.task, call.prompt_version) == ("bridge_sql", "2a24f5d")
    assert (call.system, call.user) == (bridges.SYSTEM, bridges.build_prompt(PLAYLIST_TRACK))
    assert "Join the bridge table to each related entity table" in call.user


def test_several_candidates_are_listed_and_claude_names_the_owner() -> None:
    gateway = ScriptedGateway(lambda task, user: proposal(" track "))

    got = asyncio.run(bridges.propose(gateway, PLAYLIST_TRACK, [PLAYLIST, TRACK]))

    assert got is not None and got[1] == TRACK  # matched ignoring case and space
    [call] = gateway.calls
    assert (call.prompt_version, call.schema) == (
        "2a24f5d+owner.1",
        OwnedBridgeSqlAttributeProposal,
    )
    assert call.user.startswith(bridges.build_prompt(PLAYLIST_TRACK))
    assert "  - Playlist: A named list of tracks.\n  - Track\n" in call.user


def test_an_owner_that_is_not_a_candidate_falls_back_to_the_first() -> None:
    gateway = ScriptedGateway(lambda task, user: proposal("Album"))
    got = asyncio.run(bridges.propose(gateway, PLAYLIST_TRACK, [PLAYLIST, TRACK]))
    assert got is not None and got[1] == PLAYLIST


def test_an_invalid_answer_proposes_nothing() -> None:
    gateway = ScriptedGateway(lambda task, user: None)
    assert asyncio.run(bridges.propose(gateway, PLAYLIST_TRACK, [PLAYLIST, TRACK])) is None


def test_a_self_referential_bridge_is_told_to_alias_its_table_twice() -> None:
    also_buy = {
        "table_name": "also_buy",
        "fk_pairs": [
            {"source_column": "product_id", "target_table": "product", "target_column": "id"},
            {"source_column": "also_id", "target_table": "product", "target_column": "id"},
        ],
    }
    prompt = bridges.build_prompt(also_buy)
    assert prompt.startswith("Bridge table: also_buy\nRelated tables: product\n")
    assert "This is a self-referential bridge" in prompt
