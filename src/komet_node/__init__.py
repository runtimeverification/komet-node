from __future__ import annotations

import sys

# Parsing and traversing the KORE world-state configuration (via pyk's recursive-descent
# KORE parser and the recursive cell rewrites in ``interpreter.py``) recurses with the depth
# and size of the term. Large real contracts produce configurations far deeper than CPython's
# default recursion limit (1000), which otherwise surfaces as a ``RecursionError`` mid-request.
# Raise the ceiling to match the rest of the K tooling (pyk sets 10**7; komet sets its own
# limit at import). This is the sole cross-cutting entry point, so setting it here covers the
# server process, direct interpreter use, and the encoders. server.py backs this with a large
# serve-thread stack so a deep term raises a catchable error rather than a SIGSEGV.
sys.setrecursionlimit(10**7)
