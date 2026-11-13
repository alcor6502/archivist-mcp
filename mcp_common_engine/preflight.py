"""The preflight scaffolding: which checks may be skipped, and the decorator
that runs one and records its outcome.

The checks themselves belong to the product — what a vault needs proved and
what a database needs proved have nothing in common. What is shared is the
harness, and the rule that every check BLOCKS.
"""

import os
import sys
import traceback

SKIP = {s.strip() for s in os.environ.get("PREFLIGHT_SKIP", "").split(",") if s.strip()}

RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def deco(fn):
        def run():
            if name in SKIP:
                RESULTS.append((name, True, "SKIPPED (PREFLIGHT_SKIP)"))
                return
            try:
                msg = fn()
                RESULTS.append((name, True, msg or "ok"))
            except Exception as e:                  # a crash counts as a failure
                RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        return run
    return deco
