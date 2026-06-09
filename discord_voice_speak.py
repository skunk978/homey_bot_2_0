"""
Generate speech WAV files and play them into a Discord voice connection.

Used by ``discord_voice_listen_process`` (separate OS process that holds the VC).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_WINDOWS_TTS = sys.platform == "win32"
if _WINDOWS_TTS:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError:
        _WINDOWS_TTS = False

_com_tls = threading.local()


def _ensure_com_initialized() -> None:
    """SAPI requires COM on the calling thread (listen process uses asyncio.to_thread)."""
    if not _WINDOWS_TTS:
        return
    if getattr(_com_tls, "initialized", False):
        return
    pythoncom.CoInitialize()
    _com_tls.initialized = True


def _pick_voice(speaker: Any, voice_hint: str) -> Any | None:
    hint = (voice_hint or "female").strip().lower()
    voices = speaker.GetVoices()
    male_kw = ("david", "mark", "james", "george", "male", "guy", "christopher", "brian")
    female_kw = ("zira", "hazel", "susan", "female", "woman", "jenny", "aria")
    keywords = male_kw if hint == "male" else female_kw
    for i in range(voices.Count):
        v = voices.Item(i)
        desc = v.GetDescription().lower()
        if any(k in desc for k in keywords):
            return v
    if voices.Count > 0:
        return voices.Item(0)
    return None


def generate_speech_wav(text: str, *, voice_hint: str = "female") -> str | None:
    """Return path to a temporary WAV file, or None on failure."""
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > 500:
        text = text[:500] + "..."

    if not _WINDOWS_TTS:
        log.error("[DiscordSpeak] Windows SAPI TTS unavailable")
        return None

    out_path = os.path.abspath(
        f"temp_discord_speak_{int(time.time())}_{abs(hash(text)) % 100000}.wav"
    )
    try:
        _ensure_com_initialized()
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        voice = _pick_voice(speaker, voice_hint)
        if voice is not None:
            speaker.Voice = voice
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(out_path, 3)  # SSFMCreateForWrite
        speaker.AudioOutputStream = stream
        speaker.Speak(text)
        stream.Close()
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 44:
            return out_path
    except Exception:
        log.exception("[DiscordSpeak] Failed to generate WAV for %r", text[:80])
    try:
        if os.path.isfile(out_path):
            os.remove(out_path)
    except OSError:
        pass
    return None


async def play_wav_on_voice_client(vc: Any, wav_path: str) -> bool:
    """Play a WAV file on an connected discord VoiceClient / VoiceRecvClient."""
    import asyncio

    import discord

    if vc is None or not getattr(vc, "is_connected", lambda: False)():
        log.warning("[DiscordSpeak] No connected voice client — cannot play %s", wav_path)
        return False
    try:
        while vc.is_playing():
            await asyncio.sleep(0.05)
        source = discord.FFmpegPCMAudio(wav_path)
        vc.play(source)
        while vc.is_playing():
            await asyncio.sleep(0.05)
        return True
    except Exception:
        log.exception("[DiscordSpeak] Playback failed: %s", wav_path)
        return False
    finally:
        try:
            if os.path.isfile(wav_path):
                os.remove(wav_path)
        except OSError:
            pass
