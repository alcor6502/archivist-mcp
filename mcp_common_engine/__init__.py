"""Common engine for MCP servers.

What lives here is what more than one server has already had to write. Nothing
in this package knows what the server it serves is FOR: no domain types, no
tool names, no storage. If a piece needs to know, it belongs to the product.

Measured against fastmcp==3.4.5. `gate` and `refusals` depend on where that
version routes messages and where it logs them, so the pin is part of the
contract and not a convenience.

`Gate` and `make_tool` are NOT re-exported here, on purpose: importing this
package must not drag fastmcp in. A preflight has to be able to run — and to
report — on an image where fastmcp is missing or broken, which is one of the
things it exists to catch. Import them from their own modules.
"""

from .config import (
    DEFAULT_CIDRS,
    LOG_LEVELS,
    cidrs_from_env,
    describe_cidrs,
    is_placeholder,
    log_level_from_env,
    parse_cidrs,
)
from .preflight import RESULTS, SKIP, check

VERSION = "1.3.0"

__all__ = [
    "VERSION",
    "DEFAULT_CIDRS", "LOG_LEVELS",
    "cidrs_from_env", "describe_cidrs", "is_placeholder",
    "log_level_from_env", "parse_cidrs",
    "RESULTS", "SKIP", "check",
]
