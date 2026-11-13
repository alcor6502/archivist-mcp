"""A regular expression the caller wrote, run with a clock on it.

Three places run a pattern that arrives in a call: `search(regex=True)`, the
`forbid` of render_pdf and the `forbid` of render_report. With the standard
`re` none of them could be stopped: `(a|aa)+$` against sixty `a` and a `b`
does not return in any time worth waiting for, and the thread that runs it is
gone until the container restarts. `re` has no timeout and a thread cannot be
killed from outside; the `regex` module has one, checked inside the matching
loop, and reads the same syntax.

One number, one place: REGEX_SECONDS is the budget of a whole CALL, not of one
match — a pattern that costs a millisecond per line and meets a million lines
is the same lost thread. A `Budget` is made when the call starts and every
match is given what is left of it.

Nothing here knows about vaults or PDFs. Each caller turns `RegexTimeout`
into its own refusal, in its own words.
"""
from __future__ import annotations

import time

import regex

REGEX_SECONDS = 5.0

error = regex.error


class RegexTimeout(Exception):
    """The pattern used up the call's budget. The caller's to simplify."""


def compile(pattern: str, ignore_case: bool = False):
    """Raises `rxlimit.error` on a pattern that does not parse."""
    return regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)


class Budget:
    def __init__(self) -> None:
        self.seconds = REGEX_SECONDS
        self._deadline = time.monotonic() + self.seconds

    def search(self, rx, text: str) -> bool:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise RegexTimeout(rx.pattern)
        try:
            return rx.search(text, timeout=left) is not None
        except TimeoutError:
            raise RegexTimeout(rx.pattern) from None
