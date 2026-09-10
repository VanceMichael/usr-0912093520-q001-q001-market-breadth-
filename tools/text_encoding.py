"""Portable decoding for small user-edited configuration files."""

from __future__ import annotations

from pathlib import Path


def read_portable_text(path: Path) -> str:
    """Read UTF text while accepting encodings commonly produced on Windows."""
    payload = path.read_bytes()
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as utf8_error:
        try:
            return payload.decode("gb18030")
        except UnicodeDecodeError:
            raise utf8_error
