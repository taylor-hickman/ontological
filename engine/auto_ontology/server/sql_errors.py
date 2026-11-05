# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Added by Ontological: upstream's SqlParseError, moved out of server/sql_utils.py.

"""The one error the DAL takes from ``server/sql_utils.py``.

Upstream defines it there, and importing that module loads every connector,
the sqlglot parsers and ``retrieval/``. ``dal/custom_analyses.py`` and
``dal/sql_attributes.py`` import it from here instead, so the DAL loads on its
own.
"""


class SqlParseError(Exception):
    """SQL could not be parsed or resolved against the catalog."""
