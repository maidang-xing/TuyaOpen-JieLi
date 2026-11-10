"""Keep every staged write inside the staging tree.

``create_staging_tree()`` copies the subtrees the build rewrites and links the
rest to the pristine SDK (``include_lib``, ``lib``, ``tools``,
``cpu/<chip>/liba`` and most of ``apps/common``). A staging path looks the same
either way, so a hook that opens one for writing edits
``chip/<chip>/<sdk>/...`` in place - a probe once landed in the vendor's
``lcd_driver.c`` that way, and only ``git status`` showed it.

``materialize()`` replaces the highest link on the path with a real copy before
the write; every staged write goes through ``write_staged_text()``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return bool(isjunction(path))
    return path.is_dir() and Path(os.path.realpath(path)) != Path(os.path.abspath(path))


def _linked_ancestor(path: Path) -> Optional[Path]:
    """The highest link between `path` and the filesystem root, if any.

    The highest one matters: replacing a nested link leaves the outer link in
    place, and the write would still end up in the vendor tree.
    """
    linked = None
    for candidate in (path, *path.parents):
        if _is_link(candidate):
            linked = candidate
    return linked


def _unlink(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


def materialize(path: Path) -> None:
    """Replace a link on `path` with a real copy so writes stay in staging."""
    linked = _linked_ancestor(path)
    if linked is None:
        return
    target = Path(os.path.realpath(linked))
    if target.is_dir():
        copy = linked.with_name(linked.name + ".materialized")
        if copy.exists():
            shutil.rmtree(copy)
        shutil.copytree(target, copy, ignore_dangling_symlinks=True)
        _unlink(linked)
        copy.rename(linked)
        return
    content = target.read_bytes()
    _unlink(linked)
    linked.write_bytes(content)


def write_staged_text(path: Path, content: str) -> None:
    materialize(path)
    path.write_text(content, encoding="utf-8")


def write_staged_bytes(path: Path, content: bytes) -> None:
    materialize(path)
    path.write_bytes(content)
