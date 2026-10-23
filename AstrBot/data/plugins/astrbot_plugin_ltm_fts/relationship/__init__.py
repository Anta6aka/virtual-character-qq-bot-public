"""Cross-scene relationship and memory-boundary module for the ltm plugin.

Pure logic lives in `identity`, `visibility`, `engine`, and `context`.
Persistence and side effects live in `store` and `service`.
The module is intentionally importable without AstrBot so it can be unit
tested against temporary databases; integration happens in ``main.py``.
"""
from __future__ import annotations

__all__ = [
    "params",
    "identity",
    "visibility",
    "engine",
    "context",
    "store",
    "service",
]
