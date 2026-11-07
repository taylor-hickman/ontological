"""An InterchangeService without a store, for the app's end-to-end tests (apps/desktop/test/e2e).

An export answers with a small document naming what it was asked for: YAML with `format` and
`databases`, or with `include_ontology` a zip of the three Ossie files. An import reads the
document's own `databases` (each a mapping with a `name`), `created` and `skipped` counts, and
answers with them, starting a re-embed job for each database through the jobs service, as the
store's import does. A document with an `error` key is refused with that message (422), and one
whose root is `semantic_model` is read as Ossie. Each export and import is appended to
<appData>/interchange.log.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, cast

import yaml

from ontological.interchange.schemas import ExportRequest, ModelFormat
from ontological.interchange.service import OSSIE_EXPORT_FILES
from ontological.interchange.store import Exported, Imported
from ontological.jobs.service import JobsService

KINDS = (
    "databases",
    "schemas",
    "tables",
    "columns",
    "terms",
    "column_attributes",
    "sql_attributes",
    "custom_analyses",
)


class FakeInterchange:
    def __init__(self, app_data: Path, jobs: JobsService) -> None:
        self._log = app_data / "interchange.log"
        self._jobs = jobs

    def _note(self, line: str) -> None:
        with open(self._log, "a") as f:
            f.write(line + "\n")

    async def export(self, request: ExportRequest) -> Exported:
        scope = ",".join(request.databases) or "*"
        ontology = " ontology" if request.include_ontology else ""
        self._note(f"export {request.format.value} {scope}{ontology}")
        document = yaml.safe_dump({"format": request.format.value, "databases": request.databases})
        if request.include_ontology:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name in OSSIE_EXPORT_FILES:
                    archive.writestr(name, document)
            return Exported(buffer.getvalue(), "application/zip", "ossie-model.zip")
        name = "ossie" if request.format is ModelFormat.OSSIE else "ontological"
        return Exported(document.encode(), "application/x-yaml", f"{name}-model.yaml")

    async def import_model(self, yaml_text: str, *, replace: bool) -> Imported:
        payload = yaml.safe_load(yaml_text)
        if not isinstance(payload, dict):
            raise ValueError("YAML document must deserialize to a mapping")
        document = cast(dict[str, Any], payload)
        self._note(f"import replace={replace}")
        if "error" in document:
            raise ValueError(str(document["error"]))
        names = [str(d["name"]) for d in document.get("databases", [])]
        created: dict[str, int] = document.get("created", {})
        skipped: dict[str, int] = document.get("skipped", {})
        summary = {
            "created": {k: int(created.get(k, 0)) for k in KINDS},
            "skipped": {k: int(skipped.get(k, 0)) for k in KINDS},
            "replace": replace,
            "format": "ossie" if "semantic_model" in document else "auto_ontology",
        }
        for name in names:
            await self._jobs.reembed(name)
        return Imported(summary=summary, reembedding=names)
