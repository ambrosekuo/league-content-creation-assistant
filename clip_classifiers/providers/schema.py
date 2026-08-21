"""JSON schema for AI structured classification output."""

from __future__ import annotations

from clip_classifiers.taxonomy import HOOK_STYLES, PRIMARY_CATEGORIES

CLASSIFICATION_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "primary": {
            "type": "string",
            "enum": list(PRIMARY_CATEGORIES),
        },
        "secondary": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": list(PRIMARY_CATEGORIES),
            },
            "maxItems": 3,
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "reason": {"type": "string"},
        "hook_style": {
            "type": "string",
            "enum": list(HOOK_STYLES),
        },
    },
    "required": ["primary", "secondary", "confidence", "reason", "hook_style"],
    "additionalProperties": False,
}
