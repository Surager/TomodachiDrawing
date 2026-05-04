"""Resolve nxbt from the vendored tree under ``third_party/nxbt`` (or ``NXBT_SOURCE_DIR``)."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_on_path = False


def _nxbt_parent_dir() -> Path:
    override = os.environ.get("NXBT_SOURCE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root / "third_party" / "nxbt").resolve()


def ensure_nxbt_on_path() -> None:
    """Insert the directory that contains the ``nxbt`` package at the front of ``sys.path``."""
    global _on_path
    if _on_path:
        return
    parent = _nxbt_parent_dir()
    pkg = parent / "nxbt"
    if not pkg.is_dir():
        raise ModuleNotFoundError(
            f"nxbt package not found at {pkg}. "
            "Place a copy of nxbt under third_party/nxbt (see NOTICE), "
            "or set NXBT_SOURCE_DIR to a directory that contains an nxbt package folder."
        )
    sys.path.insert(0, str(parent))
    _on_path = True


def import_nxbt():
    """Import the ``nxbt`` module (vendored copy by default)."""
    ensure_nxbt_on_path()
    return importlib.import_module("nxbt")
