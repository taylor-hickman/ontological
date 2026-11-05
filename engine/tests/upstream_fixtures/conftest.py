"""Upstream's DAL and catalog-store tests, run by tests/golden_dal/test_golden_dal.py."""

import overlay

overlay.install()

# test_model_interchange.py tests dal/model_interchange.py, which isn't vendored: M6.4 ports it
# with server/model_interchange/* (ARCHITECTURE.md §11). Until then neither is run, and
# dal_surface.json leaves the module out. M6.4 deletes this line and restores both.
collect_ignore = ["auto_ontology/dal/tests/test_model_interchange.py"]
