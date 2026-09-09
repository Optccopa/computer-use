"""Entry point for `python -m cufast.server`, which is how the README wires it up.

A package needs this file to be runnable that way; `if __name__ == "__main__"` in
__init__ is not reached by -m, so without it the documented command fails with no
obvious cause.
"""

from __future__ import annotations

from cufast.server.app import main

main()
