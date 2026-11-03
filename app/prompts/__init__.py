"""Prompt loader. Prompts are editable Markdown files in this directory."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache
def load(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
