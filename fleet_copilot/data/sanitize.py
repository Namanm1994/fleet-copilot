"""Telemetry strings come from devices and are untrusted. Clean them once, on load."""

from __future__ import annotations

import re
import unicodedata

MAX_LEN = 100


def clean_text(value: object, max_len: int = MAX_LEN) -> str | None:
    """Strip control and format characters (incl. zero-width), collapse whitespace, cap length."""
    if value is None:
        return None
    text = str(value)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def normalize_name(value: str | None) -> str:
    """Lowercase alphanumeric form used for matching (µTorrent -> utorrent)."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value.replace("µ", "u").replace("μ", "u"))
    return re.sub(r"[^a-z0-9]", "", text.lower())
