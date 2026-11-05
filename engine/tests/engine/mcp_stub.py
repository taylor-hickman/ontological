"""ontological-mcp with serve_stub.py's engine, for the shim tests.

    mcp_stub.py <the shim's arguments>

It runs `ontological.mcp.main.main` with the same arguments, starting engines with serve_stub.py
in place of `python -m ontological.engine`, so they have its fake store.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ontological.mcp import main

if __name__ == "__main__":
    serve_stub = Path(__file__).with_name("serve_stub.py")
    sys.exit(main.main(sys.argv[1:], engine=[sys.executable, str(serve_stub)]))
