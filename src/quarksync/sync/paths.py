from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_INVALID = re.compile(r'[<>:"\\|?*\x00-\x1f]')


class UnmappableWindowsPath(ValueError):
    pass


def normalize_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    parts = PurePosixPath(normalized).parts if normalized else ()
    if any(part in {"", ".", ".."} for part in parts):
        raise UnmappableWindowsPath("Path contains an unsafe segment")
    return "/".join(parts)


def validate_windows_relative_path(path: str) -> str:
    normalized = normalize_relative_path(path)
    for segment in PurePosixPath(normalized).parts:
        if _INVALID.search(segment):
            raise UnmappableWindowsPath(f"Windows-invalid character in {segment!r}")
        if segment.endswith((" ", ".")):
            raise UnmappableWindowsPath(f"Trailing dots/spaces are unsupported: {segment!r}")
        if segment.split(".", 1)[0].upper() in _RESERVED:
            raise UnmappableWindowsPath(f"Windows reserved name: {segment!r}")
    return normalized


def local_path(root: Path, relative_path: str) -> Path:
    normalized = validate_windows_relative_path(relative_path)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    if root.resolve() not in candidate.resolve(strict=False).parents:
        raise UnmappableWindowsPath("Path escapes the sync root")
    return candidate


def key(path: str) -> str:
    return normalize_relative_path(path).casefold()

