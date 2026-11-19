# Selected MIT source from Floxizdia/hoi4-modmaker, revision a5c138ffacf4ed471769558f864cbe55ec5163fa.
# See LICENSE and ORIGIN.json.
import re

_BALANCE_SCAN_RE = re.compile(r'[\n{}"\\#]')

def _brace_balance(text):
    """Return (depth_at_end, line_of_first_negative). Respects quotes.

    Jumps between the characters that matter rather than stepping over
    every one; this runs over every script file in the mod, and the
    character walk it replaces was the second-largest cost in a full
    validation."""
    depth = 0
    first_neg = None
    line = 1
    in_quotes = False
    i = 0
    n = len(text)
    search = _BALANCE_SCAN_RE.search
    while i < n:
        match = search(text, i)
        if match is None:
            break
        i = match.start()
        ch = text[i]
        if ch == "\n":
            line += 1
        elif in_quotes:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_quotes = False
        elif ch == '"':
            in_quotes = True
        elif ch == "#":
            newline = text.find("\n", i)
            i = n if newline == -1 else newline
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0 and first_neg is None:
                first_neg = line
        i += 1
    return depth, first_neg
