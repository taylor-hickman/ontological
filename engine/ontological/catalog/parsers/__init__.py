"""SQL parsers for query history, ported from upstream's catalog/parsers. They run in the parse
pool's workers (`catalog/parse_pool.py`), so nothing here imports the store."""
