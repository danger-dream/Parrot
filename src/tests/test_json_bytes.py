import json
import random

import pytest

from src import json_bytes

_TEXT = ["", "a", "plain ascii", "中文内容", "emoji 🦜✅", "quote \" back \\ slash", "ctl \x00\x1f\n\t\r\b\f\x7f",
         "\u2028\u2029", "é" * 300, "x" * 70000, "🦜" * 20000]
_SCALARS = [0, -1, 2**70, 1.5, 1e-05, 1e16, 0.1 + 0.2, float("inf"), float("-inf"), True, False, None]


def _random_value(rng, depth):
    roll = rng.random()
    if depth <= 0 or roll < 0.35:
        return rng.choice(_TEXT + _SCALARS)
    if roll < 0.7:
        return {rng.choice(_TEXT[:7]) + str(i): _random_value(rng, depth - 1) for i in range(rng.randint(0, 6))}
    items = [_random_value(rng, depth - 1) for _ in range(rng.randint(0, 8))]
    return tuple(items) if rng.random() < 0.2 else items


@pytest.mark.parametrize("sort_keys", [False, True])
@pytest.mark.parametrize("separators", [None, (",", ":"), (", ", ": ")])
def test_random_structures_are_byte_identical(separators, sort_keys):
    rng = random.Random(7)
    for _ in range(300):
        value = _random_value(rng, 5)
        expected = json.dumps(value, ensure_ascii=False, separators=separators, sort_keys=sort_keys).encode("utf-8")
        assert json_bytes.dumps_utf8(value, separators=separators, sort_keys=sort_keys) == expected


def test_large_request_shapes_are_byte_identical():
    body = {
        "model": "m", "stream": True, "max_tokens": 32000,
        "system": [{"type": "text", "text": "系统提示 🦜 " * 5000}],
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": [
                    {"type": "text", "text": "输出 " * 4000},
                    {"type": "image", "source": {"type": "base64", "data": "QUJD" * 100000}},
                ]},
            ]}
            for i in range(6)
        ],
        "tools": [{"name": f"T{i}", "input_schema": {"type": "object", "minimum": 1e-05}} for i in range(30)],
    }
    for separators in (None, (",", ":")):
        expected = json.dumps(body, ensure_ascii=False, separators=separators).encode("utf-8")
        assert json_bytes.dumps_utf8(body, separators=separators) == expected


def test_non_string_keys_fall_back_to_json_semantics():
    value = {"big": "x" * 70000, "nested": {1: "a", 2.5: "b", True: "c", None: "d"}}
    expected = json.dumps(value, ensure_ascii=False).encode("utf-8")
    assert json_bytes.dumps_utf8(value) == expected
    top = {1: "x" * 70000, "a": [1, 2]}
    assert json_bytes.dumps_utf8(top) == json.dumps(top, ensure_ascii=False).encode("utf-8")


def test_errors_match_json_dumps():
    cyclic = {"big": "x" * 70000, "items": []}
    cyclic["items"].append(cyclic)
    with pytest.raises(ValueError, match="Circular reference"):
        json.dumps(cyclic, ensure_ascii=False)
    with pytest.raises(ValueError, match="Circular reference"):
        json_bytes.dumps_utf8(cyclic)

    surrogate = {"big": "x" * 70000, "text": "\ud83d"}
    with pytest.raises(UnicodeEncodeError):
        json.dumps(surrogate, ensure_ascii=False).encode("utf-8")
    with pytest.raises(UnicodeEncodeError):
        json_bytes.dumps_utf8(surrogate)

    unserializable = {"big": "x" * 70000, "value": object()}
    with pytest.raises(TypeError):
        json_bytes.dumps_utf8(unserializable)


def test_log_body_storage_matches_previous_encoding():
    from src import log_db

    def previous(value):
        return log_db._encode_log_body(json.dumps(value, ensure_ascii=False))

    cases = [
        {"model": "m", "messages": [{"role": "user", "content": "hi 🦜"}]},
        {"model": "m", "messages": [{"role": "user", "content": "长文本 " * 5000 + "🦜"}]},
        {"model": "m", "messages": [{"role": "user", "content": "QUJD" * 300000}]},
        {"model": "m", "messages": [{"role": "user", "content": "x" * 70000 + "\ud83d"}]},
    ]
    for value in cases:
        assert log_db._encode_log_body_value(value) == previous(value)



_PARSE_CASES = [
    '{"a":"🦜"}', '{"a":"x🦜y🐜z","b":["😀",{"c":"𝒳"}]}', '🦜', '[1, 🦜]', '{"a":"\\🦜"}', '{"a":"\\\\🦜"}',
    '{"a":"\\ud83d🦜"}', '{"a":"\\ud83d"}', '{"a":"🦜\\udc1c"}', '{"🦜":1,"k🐜":"v"}', '"🦜"', '"🦜',
    '{"a":"🦜"} x', '{"a":"中文 🦜 ✅ é"}', '{"a":"' + "🦜" * 5000 + '"}', '[NaN, 1e400, "🦜"]',
]


@pytest.mark.parametrize("text", _PARSE_CASES)
def test_loads_utf8_matches_json_loads(text):
    raw = text.encode("utf-8")
    try:
        expected = ("ok", json.dumps(json.loads(raw)))
    except ValueError as exc:
        expected = ("err", type(exc), str(exc))
    try:
        got = ("ok", json.dumps(json_bytes.loads_utf8(raw)))
    except ValueError as exc:
        got = ("err", type(exc), str(exc))
    assert got == expected


@pytest.mark.parametrize("raw", [
    b'\xef\xbb\xbf{"a":"\xf0\x9f\xa6\x9c"}',
    '{"a":"🦜"}'.encode("utf-16"),
    b'{"a":"\xf0\x9f\xa6"}',
    b'{"a":"\xf0\x9f\xa6\x9c\xff"}',
    b'{"a":"\xed\xa0\xbd\xf0\x9f\xa6\x9c"}',
    b'{"a":"\xf4\x90\x80\x80"}',
])
def test_loads_utf8_keeps_stdlib_behaviour_for_unusual_bytes(raw):
    try:
        expected = ("ok", json.dumps(json.loads(raw)))
    except ValueError as exc:
        expected = ("err", type(exc), str(exc))
    try:
        got = ("ok", json.dumps(json_bytes.loads_utf8(raw)))
    except ValueError as exc:
        got = ("err", type(exc), str(exc))
    assert got == expected


def test_loads_utf8_parses_a_narrow_document():
    raw = json.dumps({"text": "x" * 100000 + "🦜"}, ensure_ascii=False).encode("utf-8")
    narrowed = json_bytes._escape_four_byte_chars(raw)
    assert narrowed is not None and max(map(ord, narrowed)) <= 0xFFFF
    assert json_bytes.loads_utf8(raw) == json.loads(raw)
