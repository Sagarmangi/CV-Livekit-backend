"""FSD human-likeness requirement: when a caller asks "are you AI?" / "am I
talking to a robot?", answer from a rotating set so consecutive calls (and
repeat asks within one call) don't hear the same line twice in a row.

NOTE: exact required wording (including any legally-mandated AI-disclosure
phrasing per FSD Section 4.5) isn't available to me -- these are reasonable
placeholder lines. Section 4.5 disclosure is a compliance question that needs
legal sign-off per jurisdiction before launch (see Phase 9); don't treat this
list as satisfying that requirement without that review.
"""

from __future__ import annotations

import itertools
import threading

from .state import CallState

DEFLECTION_LINES: list[str] = [
    "I'm an automated assistant for Kodexo Labs -- I'm here to get some quick details so the "
    "right person can follow up with you.",
    "I'm a virtual assistant, yes -- happy to help get you to the right person, though.",
    "I'm an AI assistant helping out on this line. Let's get you sorted -- what can I help with?",
]

_lock = threading.Lock()
_global_offset = itertools.count()


def start_call_offset() -> int:
    """Called once per call so consecutive calls don't all start on line 0."""
    with _lock:
        return next(_global_offset) % len(DEFLECTION_LINES)


def next_deflection_line(state: CallState) -> str:
    line = DEFLECTION_LINES[state.ai_deflection_index % len(DEFLECTION_LINES)]
    state.ai_deflection_index += 1
    return line
