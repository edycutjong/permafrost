"""Typed action tools for Qwen function calling (SPEC §6 action surface).

Actions are typed tool calls — auditable and testable — never prompt-parsed
"please alarm" text. The live diagnosis call passes these definitions; the
verdict's ``actions`` array uses the same names, so edge dispatch is one map.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ACTION_TOOL_DEFS"]

ACTION_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "sound_alarm",
            "description": "Sound the local piezo siren on the edge device immediately.",
            "parameters": {
                "type": "object",
                "properties": {"now": {"type": "boolean", "description": "Fire immediately."}},
                "required": ["now"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify",
            "description": "Notify a human through the configured channel (mock provider in MVP).",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["phone", "email", "service"]},
                    "note": {"type": "string"},
                },
                "required": ["channel"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "annotate_log",
            "description": "Append an annotation entry to the tamper-evident hash-chain log.",
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_service",
            "description": "Open a maintenance/service request for the storage unit.",
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_edge_rules",
            "description": "Propose shipping a new signed local rule bundle to the edge.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": [],
            },
        },
    },
]
