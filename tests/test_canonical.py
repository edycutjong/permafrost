"""Canonical JSON — the byte-representation every hash and signature depends on."""

import json

from permafrost.canonical import canonical_dumps, canonical_json


def test_keys_sorted_at_every_level():
    assert canonical_dumps({"b": 1, "a": {"z": 1, "y": 2}}) == '{"a":{"y":2,"z":1},"b":1}'


def test_no_whitespace_separators():
    s = canonical_dumps({"a": [1, 2, 3], "b": "x"})
    assert " " not in s and s == '{"a":[1,2,3],"b":"x"}'


def test_unicode_not_escaped_utf8_bytes():
    b = canonical_json({"t": "2–8°C"})
    assert "2–8°C".encode("utf-8") in b


def test_stable_across_calls():
    obj = {"z": 1.5, "a": [{"k": True, "b": None}]}
    assert canonical_json(obj) == canonical_json(json.loads(canonical_dumps(obj)))


def test_insertion_order_irrelevant():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_float_repr_roundtrip():
    obj = {"v": 4.123}
    assert json.loads(canonical_dumps(obj))["v"] == 4.123
