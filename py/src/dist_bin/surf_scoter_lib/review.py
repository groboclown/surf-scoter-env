#SPDX:MIT
"""Showing the project's text safely: in a terminal, a report, or a patch.

Anything a project wrote can carry terminal escape sequences, control
characters, or characters that pose as others.  `clean` makes text fit for
one line of output; `visible` and `file_lines` show a file's contents with
those characters escaped, not removed, split as Docker and the shell split
them; `scalar` writes a value as JSON would, ASCII only.
"""
import json
import re

from . import jdata

# Terminal escape sequences: CSI (`ESC [ ... final`), OSC (`ESC ] ... BEL` or
# `ESC ] ... ESC \\`), and two-character escapes.
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def clean(text: str) -> str:
    """`text` for one comment line, or the terminal.

    Terminal escape sequences and other control characters go (line breaks
    and tabs become spaces), and anything outside ASCII shows as a `\\u`
    escape, so nothing hides, or poses as something else.
    """
    text = _ESCAPES.sub("", text)
    text = re.sub(r"[\r\n\t]", " ", text)
    out: list[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 or 0x7F <= code < 0xA0:
            continue
        if code < 0x7F:
            out.append(char)
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


def visible(text: str) -> str:
    """`text` with its control characters shown, as `\\xNN` (or `\\uNNNN`), not removed.

    For file contents shown in the review: `clean` would delete a lone
    carriage return, which joins two pieces of a line a reader might take
    for two lines (VER-1).  Tabs stay.
    """
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char != "\t" and (code < 0x20 or 0x7F <= code < 0xA0):
            out.append(f"\\x{code:02x}")
        elif code in (0x2028, 0x2029, 0xFEFF):
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def file_lines(text: str) -> list[str]:
    """`text` split as Docker and the shell split it, on `\\n` only, each line made `visible`.

    The `\\r` of a `\\r\\n` goes, as Docker drops it.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [visible(line.removesuffix("\r")) for line in lines]


def scalar(value: jdata.JsonData) -> str:
    """A scalar as YAML: text exactly as JSON writes it (ASCII, escaped), the rest as JSON."""
    return json.dumps(value, ensure_ascii=True)
