"""JSON for multi-MiB request bodies without four-byte-wide whole-document strings.

The stdlib handles a whole document as one str whose storage width is set by
its widest character: one emoji turns a 20 MiB request body into an 80 MiB
string, both when bytes are decoded for parsing and when serializing.

``dumps_utf8`` is byte-identical to ``json.dumps(obj, ensure_ascii=False,
...).encode("utf-8")`` but encodes large containers member by member, so each
temporary string is only as large (and as wide) as one member.  Anything the
piecewise path cannot reproduce exactly (non-string keys, cycles) is handed to
json.dumps unchanged.

``loads_utf8`` returns what ``json.loads(data)`` returns, but first rewrites the
rare four-byte UTF-8 characters as the equivalent JSON surrogate-pair escapes,
so the decoded document is at most two bytes per character.  Errors are raised
by json.loads on the original bytes.
"""

from __future__ import annotations

import io
import json
from typing import Any

# Containers estimated below this many characters are encoded in one call.
_PIECE_CHARS = 64 * 1024

_FOUR_BYTE_LEADS = (b"\xf0", b"\xf1", b"\xf2", b"\xf3", b"\xf4")


def loads_utf8(data: bytes) -> Any:
    narrowed = _escape_four_byte_chars(data) if isinstance(data, bytes) else None
    if narrowed is None:
        return json.loads(data)
    try:
        return json.loads(narrowed)
    except ValueError:
        return json.loads(data)


def _escape_four_byte_chars(data: bytes) -> str | None:
    """Decoded ``data`` with U+10000.. written as escapes, or None to parse as-is.

    An escape is only equivalent inside a JSON string and not right after a
    backslash; those positions, non-UTF-8 input and undecodable sequences keep
    the original path so its result or error is unchanged.
    """
    positions = sorted(pos for lead in _FOUR_BYTE_LEADS for pos in _find_all(data, lead))
    if not positions or json.detect_encoding(data) != "utf-8":
        return None
    pieces: list[bytes] = []
    start = 0
    for pos in positions:
        if pos < start or (pos and data[pos - 1:pos] == b"\\"):
            return None
        try:
            code = ord(data[pos:pos + 4].decode("utf-8")) - 0x10000
        except (UnicodeDecodeError, TypeError):
            return None
        pieces.append(data[start:pos])
        pieces.append(b"\\u%04x\\u%04x" % (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)))
        start = pos + 4
    pieces.append(data[start:])
    joined = b"".join(pieces)
    del pieces
    try:
        return joined.decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return None


def _find_all(data: bytes, needle: bytes):
    pos = data.find(needle)
    while pos >= 0:
        yield pos
        pos = data.find(needle, pos + 1)


def dumps_utf8(obj: Any, *, separators: tuple[str, str] | None = None, sort_keys: bool = False) -> bytes:
    item_sep, key_sep = separators or (", ", ": ")
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(item_sep, key_sep), sort_keys=sort_keys)
    out = io.BytesIO()
    _write(out, obj, encoder, item_sep.encode("utf-8"), key_sep.encode("utf-8"), set())
    return out.getvalue()


def _approx_chars(obj: Any, budget: int) -> int:
    """Lower bound of the encoded size, stopping once ``budget`` is reached."""
    total = 0
    stack = [obj]
    while stack and total < budget:
        value = stack.pop()
        if isinstance(value, str):
            total += len(value) + 2
        elif isinstance(value, dict):
            total += 2
            for key, item in value.items():
                total += len(key) + 4 if isinstance(key, str) else 8
                stack.append(item)
        elif isinstance(value, (list, tuple)):
            total += 2 + len(value)
            stack.extend(value)
        else:
            total += 8
    return total


def _write(out: io.BytesIO, obj: Any, encoder: json.JSONEncoder,
           item_sep: bytes, key_sep: bytes, active: set[int]) -> None:
    piecewise = (
        isinstance(obj, (dict, list, tuple))
        and obj
        and id(obj) not in active
        and not (isinstance(obj, dict) and not all(isinstance(key, str) for key in obj))
        and _approx_chars(obj, _PIECE_CHARS) >= _PIECE_CHARS
    )
    if not piecewise:
        out.write(encoder.encode(obj).encode("utf-8"))
        return
    active.add(id(obj))
    is_dict = isinstance(obj, dict)
    out.write(b"{" if is_dict else b"[")
    # Consecutive small members are encoded together as one sub-container
    # whose brackets are dropped; the joined text is identical.
    batch: list = []
    batch_chars = 0
    first = True

    def separate() -> None:
        nonlocal first
        if not first:
            out.write(item_sep)
        first = False

    def flush() -> None:
        nonlocal batch_chars
        if batch:
            separate()
            out.write(encoder.encode(dict(batch) if is_dict else batch)[1:-1].encode("utf-8"))
            batch.clear()
        batch_chars = 0

    if not is_dict:
        members = obj
    elif encoder.sort_keys:
        members = sorted(obj.items(), key=lambda item: item[0])
    else:
        members = obj.items()
    for member in members:
        value = member[1] if is_dict else member
        size = _approx_chars(value, _PIECE_CHARS)
        if size < _PIECE_CHARS:
            batch.append(member)
            batch_chars += size
            if batch_chars >= _PIECE_CHARS:
                flush()
            continue
        flush()
        separate()
        if is_dict:
            out.write(encoder.encode(member[0]).encode("utf-8"))
            out.write(key_sep)
        _write(out, value, encoder, item_sep, key_sep, active)
    flush()
    out.write(b"}" if is_dict else b"]")
    active.discard(id(obj))
