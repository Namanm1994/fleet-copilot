"""Files are read and written as UTF-8 by name, never in the operating system's default encoding.

Windows defaults to a legacy code page, so a file written as UTF-8 and read back without an
encoding comes out garbled there while the same code passes on Linux.
"""
from __future__ import annotations

import ast

from fleet_copilot.paths import PROJECT_ROOT

SOURCES = sorted(p for d in ("fleet_copilot", "evals", "tests") for p in (PROJECT_ROOT / d).rglob("*.py"))
TEXT_CALLS = {"read_text", "write_text"}


def _has_encoding(call: ast.Call) -> bool:
    return any(k.arg == "encoding" for k in call.keywords)


def _binary_mode(call: ast.Call) -> bool:
    mode = next((k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None and len(call.args) >= (2 if isinstance(call.func, ast.Name) else 1):
        mode = call.args[1 if isinstance(call.func, ast.Name) else 0]
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value


def unnamed_encodings(source: str) -> list[int]:
    """Line numbers of text reads and writes that leave the encoding to the operating system."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in TEXT_CALLS and not _has_encoding(node):
            lines.append(node.lineno)
        is_open = (isinstance(f, ast.Name) and f.id == "open") or (
            isinstance(f, ast.Attribute) and f.attr == "open" and not (isinstance(f.value, ast.Name) and f.value.id in {"os", "webbrowser", "sqlite3"}))
        if is_open and not _has_encoding(node) and not _binary_mode(node):
            lines.append(node.lineno)
    return sorted(lines)


def test_the_check_finds_an_unnamed_encoding():
    assert unnamed_encodings("p.read_text()\nopen('f')\np.open('w')\n") == [1, 2, 3]
    assert unnamed_encodings("p.read_text(encoding='utf-8')\nopen('f', 'rb')\np.open('ab')\nos.open('f', 0)\n") == []


def test_every_text_file_is_opened_as_utf8():
    found = {str(p.relative_to(PROJECT_ROOT)): lines
             for p in SOURCES if (lines := unnamed_encodings(p.read_text(encoding="utf-8")))}
    assert found == {}, f"name the encoding at these lines: {found}"
