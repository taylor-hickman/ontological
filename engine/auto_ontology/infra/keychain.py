# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Added by Ontological: connectors.vault.read_secret, on the OS keychain instead of Vault.

"""Source connections kept in the OS keychain, by database name.

``dal/connections.py`` imports :func:`read_secret` from here in place of
``connectors.vault``, which needs ``hvac`` and loads every connector. The
rules are Vault's: a stored secret wins over the catalog row's connection, and
an unreadable or undecodable entry counts as no secret.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import keyring

logger = logging.getLogger(__name__)

#: The keychain service a database's connection JSON is filed under.
KEYRING_SERVICE = "Ontological"


def read_secret(database_name: str) -> dict[str, Any] | str:
    """The connection the keychain holds for *database_name*, or ``""``."""
    try:
        stored = keyring.get_password(KEYRING_SERVICE, database_name)
        return json.loads(stored) if stored else ""
    except Exception:
        logger.warning(
            "Could not read the keychain entry for database %r (service=%s); "
            "treating as no secret",
            database_name,
            KEYRING_SERVICE,
            exc_info=True,
        )
        return ""
