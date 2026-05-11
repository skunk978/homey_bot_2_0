"""
Legacy shim — Discord VC transcription runs in separate processes:

- ``discord_voice_listen_process.listen_process_entry`` — Discord gateway + voice RX → Queue[WAV]
- ``discord_transcribe_process.transcribe_process_entry`` — Whisper + Discord REST posts

Spawned from ``TwitchBot.event_ready`` when ``discord_voice_transcribe.enabled`` is true.

Shared helpers live in ``discord_voice_common``.
"""

from discord_voice_common import pcm_to_wav as _pcm_to_wav

__all__ = ("_pcm_to_wav",)
