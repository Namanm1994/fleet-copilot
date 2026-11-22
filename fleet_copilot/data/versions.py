"""OS version parsing per platform. String comparison would put "9" above "15"."""

from __future__ import annotations

import re

PLATFORM_ALIASES = {
    "darwin": "darwin",
    "macos": "darwin",
    "mac": "darwin",
    "osx": "darwin",
    "win32": "win32",
    "windows": "win32",
    "win": "win32",
}

_WIN_FEATURE = re.compile(r"^(\d{2})h([12])$", re.IGNORECASE)


def normalize_platform(value: str | None) -> str | None:
    if not value:
        return None
    return PLATFORM_ALIASES.get(value.strip().lower())


def parse_version(platform: str | None, text: str | None) -> tuple[int, ...] | None:
    """Parse an OS version into a comparable tuple.

    darwin: "14.5" -> (14, 5, 0); "15" -> (15, 0, 0)
    win32:  "11 22H2" -> (11, 22, 2); "10" -> (10, 0, 0)
    Returns None when the text can't be parsed.
    """
    plat = normalize_platform(platform)
    if not text or plat is None:
        return None
    text = text.strip()
    if plat == "darwin":
        parts = text.split(".")
        try:
            nums = [int(p) for p in parts[:3]]
        except ValueError:
            return None
        while len(nums) < 3:
            nums.append(0)
        return tuple(nums)
    tokens = text.replace(",", " ").split()
    if not tokens:
        return None
    try:
        major = int(tokens[0])
    except ValueError:
        return None
    if len(tokens) > 1:
        m = _WIN_FEATURE.match(tokens[1])
        if m:
            return (major, int(m.group(1)), int(m.group(2)))
    return (major, 0, 0)


def format_version(platform: str, version: tuple[int, ...]) -> str:
    if normalize_platform(platform) == "win32":
        major, year, half = (list(version) + [0, 0])[:3]
        return f"{major} {year}H{half}" if year else str(major)
    return ".".join(str(v) for v in version).removesuffix(".0").removesuffix(".0")
