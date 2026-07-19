"""Safe rendering of upstream AUR recipe diffs."""

from __future__ import annotations

import difflib
import unicodedata
from pathlib import PurePosixPath
from typing import Iterable

from .models import ReviewError


ANSI_RESET = "\x1b[0m"
ANSI_BOLD_CYAN = "\x1b[1;36m"
ANSI_CYAN = "\x1b[36m"
ANSI_GREEN = "\x1b[32m"
ANSI_RED = "\x1b[31m"
ANSI_YELLOW = "\x1b[33m"


def sanitize_untrusted(text: str) -> str:
    """Make attacker-controlled text inert while preserving readable layout."""
    output: list[str] = []
    for character in text:
        if character in {"\n", "\t"}:
            output.append(character)
            continue
        category = unicodedata.category(character)
        if category in {"Cc", "Cf"}:
            codepoint = ord(character)
            output.append(f"<U+{codepoint:04X}>")
        else:
            output.append(character)
    return "".join(output)


def sanitize_untrusted_line(text: str) -> str:
    """Make attacker-controlled text safe for a single terminal line."""
    output: list[str] = []
    for character in text:
        if unicodedata.category(character) in {"Cc", "Cf"}:
            output.append(f"<U+{ord(character):04X}>")
        else:
            output.append(character)
    return "".join(output)


def validate_recipe_path(name: str) -> str:
    """Return a normalized safe recipe path or reject it."""
    if not name or "\\" in name or "\x00" in name:
        raise ReviewError(f"unsafe recipe path: {name!r}")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in name):
        raise ReviewError(f"unsafe recipe path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReviewError(f"unsafe recipe path: {name!r}")
    normalized = path.as_posix()
    if normalized != name:
        raise ReviewError(f"non-normalized recipe path: {name!r}")
    return normalized


def unified(old: Iterable[str], new: Iterable[str], old_name: str, new_name: str) -> str:
    output: list[str] = []
    for line in difflib.unified_diff(
        list(old), list(new), fromfile=old_name, tofile=new_name, lineterm="\n"
    ):
        if line.startswith(("---", "+++")):
            color = ANSI_BOLD_CYAN
        elif line.startswith("@@"):
            color = ANSI_CYAN
        elif line.startswith("+"):
            color = ANSI_GREEN
        elif line.startswith("-"):
            color = ANSI_RED
        else:
            output.append(line)
            continue
        ending = "\n" if line.endswith("\n") else ""
        content = line[:-1] if ending else line
        output.append(f"{color}{content}{ANSI_RESET}{ending}")
    return "".join(output)


def source_diff(old: dict | None, new: dict) -> str:
    old_files = {
        name: entry
        for name, entry in (old.get("files", {}) if old else {}).items()
        if not name.startswith(".CI/")
    }
    new_files = {
        name: entry
        for name, entry in new.get("files", {}).items()
        if not name.startswith(".CI/")
    }
    output: list[str] = []
    for name in sorted(set(old_files) | set(new_files)):
        before = old_files.get(name)
        after = new_files.get(name)
        if before == after:
            continue
        before_text = before.get("text") if before else None
        after_text = after.get("text") if after else None
        if before_text is not None or after_text is not None:
            safe_name = sanitize_untrusted(name)
            output.append(
                unified(
                    sanitize_untrusted(before_text or "").splitlines(keepends=True),
                    sanitize_untrusted(after_text or "").splitlines(keepends=True),
                    f"a/{safe_name}",
                    f"b/{safe_name}",
                )
            )
        else:
            safe_name = sanitize_untrusted(name)
            output.append(
                f"{ANSI_YELLOW}binary {safe_name}: "
                f"{before.get('sha256') if before else '<absent>'} -> "
                f"{after.get('sha256') if after else '<absent>'}{ANSI_RESET}\n"
            )
    return "".join(output) or "(no AUR recipe changes since the reviewed baseline)\n"
