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


def software_tokens(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", name.replace("µ", "u").lower())


def software_matches(query: str, name: str) -> bool:
    """Does a software name match what the admin asked for?

    Whole words first ("chrome" matches "Google Chrome"; "go" does not). A part of a word counts
    only when the query is at least 5 characters ("torrent" matches "uTorrent"), so short queries
    cannot hit unrelated names by accident."""
    q, n = normalize_name(query), normalize_name(name)
    if not q:
        return False
    if q == n:
        return True
    qt, nt = software_tokens(query), software_tokens(name)
    if qt and any(nt[i:i + len(qt)] == qt for i in range(len(nt) - len(qt) + 1)):
        return True
    return len(q) >= 5 and q in n
