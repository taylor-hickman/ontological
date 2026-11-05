"""The local owner, and the edit checks that stand in for upstream's `server/owners.py` (§5.4).

Ontological has one user, so there are no accounts or groups to check. Rows keep upstream's
`owner_id` column, and Ontological writes `LOCAL_OWNER_ID` into it. Every edit check allows.
The names and arguments are upstream's, without FastAPI's request, so a ported caller swaps
its import and nothing else. `auto_ontology.dal.owners` and `auto_ontology.dal.groups` stay
vendored unchanged: they only read and write rows, and the engine never calls `groups`, whose
account reads need upstream's `frontend` schema.
"""

from __future__ import annotations

from auto_ontology.dal.owners import EditSubject

#: The owner id on every row Ontological writes.
LOCAL_OWNER_ID = "local"


def can_edit_entity(*, user_id: str = LOCAL_OWNER_ID, subject: EditSubject) -> bool:
    """Upstream checks the account's role and the owner group. The local user may edit."""
    return True


def require_assignable(owner_id: str | None, permission: str) -> str | None:
    """Upstream refuses an id that names no account or group. Here every id may own a row."""
    return owner_id


def enforce_edit(subject: EditSubject) -> None:
    """Upstream answers 403 when `can_edit_entity` says no. It never does here."""
