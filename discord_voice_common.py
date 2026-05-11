"""Shared helpers for split Discord voice listen / transcribe worker processes."""

from __future__ import annotations

import io
import logging
import wave
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_VOICE_RECV_PATCHED = False

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2


def load_config_dict(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def patch_voice_recv_robustness() -> None:
    """Gate RTP until SSRC is mapped; soft-fail Opus decode (discord-ext-voice_recv races)."""
    global _VOICE_RECV_PATCHED
    if _VOICE_RECV_PATCHED:
        return
    try:
        from discord.ext.voice_recv.opus import PacketDecoder
        from discord.ext.voice_recv.router import PacketRouter
        from discord.opus import OpusError
    except ImportError:
        return

    _orig_feed = PacketRouter.feed_rtp

    def feed_rtp(self, packet):  # type: ignore[no-untyped-def]
        vc = self.sink.voice_client
        if vc is not None and packet.ssrc not in vc._ssrc_to_id:
            return
        return _orig_feed(self, packet)

    PacketRouter.feed_rtp = feed_rtp  # type: ignore[method-assign]

    _orig_process = PacketDecoder._process_packet

    def _process_packet_safe(self, packet):  # type: ignore[no-untyped-def]
        try:
            return _orig_process(self, packet)
        except OpusError as e:
            log.debug("[DiscordVoice] Opus decode skipped (ssrc=%s): %s", self.ssrc, e)
            return None

    PacketDecoder._process_packet = _process_packet_safe  # type: ignore[method-assign]

    _VOICE_RECV_PATCHED = True
    log.info("[DiscordVoice] Applied voice_recv patches (SSRC gate + Opus soft-fail)")


def make_openai_client(config: dict[str, Any]):
    import importlib

    mod = importlib.import_module("homey_bot_space_lord")
    return mod._make_openai_client(config)
