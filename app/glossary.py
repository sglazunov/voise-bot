"""Post-processing glossary: fix recurring recognition errors in names/terms.

The user supplies lines mapping what Whisper *hears* to the *correct* spelling.
Accepted separators per line:  "wrong=right", "wrong->right", "wrong→right".
Lines starting with # and blank lines are ignored.

Replacements are whole-word (Unicode word boundaries) and case-insensitive,
while trying to preserve the capitalisation of the matched text for the first
letter (so "конид" -> "Коновалов" at sentence start still looks right when the
target is capitalised).
"""
from __future__ import annotations

import re
from typing import List, Tuple

_SEP = re.compile(r"\s*(?:=|->|→)\s*")


def parse(raw: str) -> List[Tuple[re.Pattern, str]]:
    """Turn the glossary text into a list of (compiled_pattern, replacement)."""
    rules: List[Tuple[re.Pattern, str]] = []
    if not raw:
        return rules
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _SEP.split(line, maxsplit=1)
        if len(parts) != 2:
            continue
        wrong, right = parts[0].strip(), parts[1].strip()
        if not wrong or not right:
            continue
        # \b doesn't always sit nicely against Cyrillic; use lookarounds on \w.
        pat = re.compile(rf"(?<!\w){re.escape(wrong)}(?!\w)", re.IGNORECASE | re.UNICODE)
        rules.append((pat, right))
    return rules


def apply(text: str, rules: List[Tuple[re.Pattern, str]]) -> str:
    for pat, right in rules:
        text = pat.sub(right, text)
    return text
