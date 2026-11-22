"""Remove key-like strings before anything is logged or stored."""

from __future__ import annotations

import os
import re

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-or-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|x-api-key)(\"?\s*[:=]\s*\"?)(bearer\s+)?[A-Za-z0-9_\-\.]{12,}"),
]
_KEY_ENVS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def redact(text: str) -> str:
    if not text:
        return text
    for env in _KEY_ENVS:
        value = os.environ.get(env)
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    for pat in _PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text
