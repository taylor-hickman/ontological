"""The split stages through the real ClaudeGateway (ARCHITECTURE.md §6.3): each one spawns on its
own route, with upstream's prompt and JSON schema. `query()` is scripted, so nothing is spawned."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sdk_fakes import HAIKU, SONNET, FakeQuery, MemoryCalls, answered, fake_claude

from ontological.compile import descriptions, keys, terms
from ontological.compile.gateway import Structured
from ontological.compile.models import (
    ColumnAttributeSpec,
    ColumnDescriptionResult,
    FkAndPkResult,
    RawTableTermsResult,
)
from ontological.llm.gateway import ClaudeGateway

TABLE = {"name": "Genre", "schema_name": "main", "pk": ["GenreId"]}
COLUMNS = [
    {"name": "GenreId", "data_type": "BIGINT"},
    {"name": "Name", "data_type": "VARCHAR"},
    {"name": "ParentId", "data_type": "BIGINT"},
]


def test_each_stage_spawns_on_its_route_with_upstream_s_prompt(tmp_path: Path) -> None:
    query = FakeQuery(
        [
            answered({"fk_suggestions": [{"column_name": "ParentId"}]}, model=SONNET),
            answered({"descriptions": [{"column_name": "Name", "description": "x"}]}, model=HAIKU),
            answered({"terms": [{"name": "Genre", "attributes": []}]}, model=SONNET),
        ]
    )
    calls = MemoryCalls()
    gateway: Structured = ClaudeGateway(
        calls=calls,
        sandbox=tmp_path / "sandbox",
        cli_path=fake_claude(tmp_path),
        query=query,
    )

    async def stages() -> None:
        found = await keys.suggest_keys(gateway, TABLE, {"columns": COLUMNS, "fks": []})
        assert [s.column_name for s in found.suggestions] == ["ParentId"]
        assert await descriptions.describe_columns(gateway, COLUMNS[1:2], {}) == {"Name": "x"}
        spec = ColumnAttributeSpec("Name", "name", datatype="VARCHAR")
        [term] = await terms.extract_terms(gateway, TABLE, [spec])
        assert term.name == "Genre"

    asyncio.run(stages())

    got = [
        (s.options.model, s.options.effort, s.options.system_prompt, s.options.output_format)
        for s in query.spawns
    ]
    assert got == [
        ("sonnet", "high", keys.SYSTEM, schema(FkAndPkResult)),
        ("haiku", "medium", descriptions.SYSTEM, schema(ColumnDescriptionResult)),
        ("sonnet", "high", terms.SYSTEM, schema(RawTableTermsResult)),
    ]
    assert [(r.task, r.prompt_version) for r in calls.rows] == [
        ("table_keys", "2a24f5d"),
        ("column_descriptions", "2a24f5d"),
        ("table_terms", "2a24f5d"),
    ]


def schema(
    model: type[FkAndPkResult | ColumnDescriptionResult | RawTableTermsResult],
) -> dict[str, object]:
    return {"type": "json_schema", "schema": model.model_json_schema()}
