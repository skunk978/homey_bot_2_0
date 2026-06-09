"""Shared LOCAL mic mute flag (GUI toggle ↔ capture thread)."""

from __future__ import annotations

import threading

_muted = threading.Event()


def is_local_mic_muted() -> bool:
    return _muted.is_set()


def set_local_mic_muted(muted: bool) -> bool:
    if muted:
        _muted.set()
    else:
        _muted.clear()
    return is_local_mic_muted()


def toggle_local_mic_muted() -> bool:
    if _muted.is_set():
        _muted.clear()
    else:
        _muted.set()
    return is_local_mic_muted()
