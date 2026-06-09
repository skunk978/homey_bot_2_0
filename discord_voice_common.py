"""Shared helpers for split Discord voice listen / transcribe worker processes."""

from __future__ import annotations

import io
import logging
import os
import sys
import wave
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


def voicemeeter_out_device_for_bus(bus: str) -> str:
    """Windows records a Voicemeeter bus via ``Voicemeeter Out *`` (not ``Input`` / VAIO)."""
    b = str(bus or "").strip().upper()
    if not b:
        return ""
    return f"Voicemeeter Out {b}"


def voicemeeter_capture_device_name(sec: dict[str, Any]) -> str:
    """Playback / remote bus for transcribe (e.g. B1 ← Discord output on VAIO only)."""
    bus = str(sec.get("voicemeeter_bus") or "").strip().upper()
    if bus:
        return voicemeeter_out_device_for_bus(bus)
    dev = str(sec.get("local_microphone_device") or "").strip()
    if dev:
        return dev
    log.warning(
        "[Voicemeeter] Set voicemeeter_bus (e.g. B1) or local_microphone_device in config.yaml"
    )
    return "Voicemeeter Out B1"


def voicemeeter_mic_capture_device_name(sec: dict[str, Any]) -> str:
    """Optional separate bus for local mic (e.g. B2) — must match Discord input device."""
    bus = str(sec.get("voicemeeter_mic_bus") or "").strip().upper()
    if bus:
        return voicemeeter_out_device_for_bus(bus)
    return ""


def speaker_gated_local_transcribe_enabled(sec: dict[str, Any]) -> bool:
    """
    Detect active VC speaker in Discord, transcribe Voicemeeter playback per user (not Opus decode).
    """
    mode = str(sec.get("discord_transcribe_mode") or "").strip().lower()
    if mode in ("speaker_local", "speaker-local", "local_per_speaker", "speaker_gated"):
        return True
    return config_bool(sec.get("discord_speaker_local_transcribe"), False)


def receive_vc_audio_enabled(sec: dict[str, Any]) -> bool:
    """Whether the VC worker decodes incoming voice (off when only Voicemeeter transcribes)."""
    if speaker_gated_local_transcribe_enabled(sec):
        return True
    if "receive_vc_audio" in sec:
        return config_bool(sec.get("receive_vc_audio"), False)
    src = str(sec.get("discord_audio_source") or "discord_vc").strip().lower()
    return src in ("discord_vc", "both")


def join_discord_voice_channel_enabled(sec: dict[str, Any]) -> bool:
    """Join configured voice channel (for TTS later) even when not receiving VC audio."""
    if config_bool(sec.get("discord_join_voice_channel"), False):
        return True
    return receive_vc_audio_enabled(sec)


def config_bool(value: Any, default: bool = False) -> bool:
    """Parse YAML/bool-ish values; avoids ``bool(\"false\")`` being True."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def install_ascii_console_logging() -> None:
    """Replace root StreamHandlers on stdout/stderr so logs never use non-ASCII on cp1252 consoles."""
    root = logging.getLogger()

    class AsciiStreamHandler(logging.StreamHandler):
        def emit(self, record):  # type: ignore[override]
            try:
                msg = self.format(record)
                line = (msg + self.terminator).encode("ascii", errors="backslashreplace").decode("ascii")
                self.stream.write(line)
                self.flush()
            except RecursionError:
                raise
            except Exception:
                self.handleError(record)

    for h in list(root.handlers):
        if not isinstance(h, logging.StreamHandler):
            continue
        stream = getattr(h, "stream", None)
        if stream is not sys.stdout and stream is not sys.stderr:
            continue
        if type(h) is AsciiStreamHandler:
            continue
        try:
            lev = h.level
            fmt = h.formatter
            root.removeHandler(h)
            nh = AsciiStreamHandler(stream)
            nh.setLevel(lev)
            if fmt is not None:
                nh.setFormatter(fmt)
            root.addHandler(nh)
        except Exception:
            pass

_VOICE_RECV_PATCHED = False

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2


def discord_snowflake_id(value: Any) -> int:
    """Normalize Discord snowflakes from YAML (quoted ids load as str)."""
    if isinstance(value, int):
        return value
    return int(str(value).strip())


async def resolve_discord_channel(client: Any, channel_id_raw: Any) -> Any | None:
    """Resolve a channel from cache or HTTP (on_ready cache can miss channels briefly)."""
    import discord

    cid = discord_snowflake_id(channel_id_raw)
    ch = client.get_channel(cid)
    if ch is not None:
        return ch
    try:
        return await client.fetch_channel(cid)
    except discord.NotFound:
        log.error("[Discord] Channel %s not found — bot may not be in that server", cid)
        return None
    except discord.Forbidden:
        log.error("[Discord] No permission to access channel %s", cid)
        return None
    except discord.HTTPException as e:
        log.error("[Discord] Could not fetch channel %s: %s", cid, e)
        return None


def log_discord_guild_hint(client: Any, expected_guild_id_raw: Any = None) -> None:
    """Log which servers the bot is in; warn if configured guild_id is missing."""
    gids = [g.id for g in client.guilds]
    summary = ", ".join(f"{g.name} ({g.id})" for g in client.guilds) if client.guilds else "(none)"
    log.info("[Discord] Bot is in %s server(s): %s", len(gids), summary)
    if not expected_guild_id_raw:
        return
    try:
        expected = discord_snowflake_id(expected_guild_id_raw)
    except (TypeError, ValueError):
        return
    if expected not in gids:
        log.error(
            "[Discord] Bot is NOT in guild %s — invite it to that server "
            "(Developer Portal → OAuth2 URL Generator: bot scope; Connect, Speak, View Channels)",
            expected,
        )


def apply_discord_token_env_override(config: dict[str, Any]) -> None:
    """Use HOMEY_DISCORD_BOT_TOKEN when set (Option B: homeybot in server, not Homeybot20)."""
    token = (os.environ.get("HOMEY_DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        return
    discord_sec = config.setdefault("discord", {})
    if not isinstance(discord_sec, dict):
        return
    discord_sec["bot_token"] = token
    log.info("[Config] discord.bot_token from HOMEY_DISCORD_BOT_TOKEN env (not config file)")


def load_config_dict(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    apply_discord_token_env_override(data)
    return data


def pcm_to_wav(pcm: bytes) -> bytes:
    return pcm_to_wav_format(pcm, rate=SAMPLE_RATE, channels=CHANNELS, sample_width=SAMPLE_WIDTH)


def pcm_rms_s16(pcm: bytes) -> float:
    """Root-mean-square level for interleaved 16-bit PCM."""
    import array

    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    return (sum(x * x for x in samples) / len(samples)) ** 0.5


def trim_pcm_speech_window(
    pcm: bytes,
    *,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    sample_width: int = SAMPLE_WIDTH,
    frame_ms: int = 20,
    rms_threshold: float = 120.0,
    hang_ms: int = 400,
) -> bytes:
    """Drop leading/trailing silence; keep speech frames plus a short hang tail."""
    if not pcm or frame_ms <= 0:
        return pcm
    frame_bytes = max(
        channels * sample_width,
        int(sample_rate * frame_ms / 1000) * channels * sample_width,
    )
    if len(pcm) < frame_bytes:
        return pcm
    first_voice: int | None = None
    last_voice: int | None = None
    for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        frame = pcm[start : start + frame_bytes]
        if pcm_rms_s16(frame) >= rms_threshold:
            idx = start // frame_bytes
            if first_voice is None:
                first_voice = idx
            last_voice = idx
    if first_voice is None or last_voice is None:
        return pcm
    hang_frames = max(1, int(hang_ms / frame_ms))
    end_frame = min(last_voice + hang_frames, (len(pcm) + frame_bytes - 1) // frame_bytes)
    trim_start = first_voice * frame_bytes
    trim_end = min(len(pcm), end_frame * frame_bytes)
    if trim_end <= trim_start:
        return pcm
    return pcm[trim_start:trim_end]


def wav_peak_amplitude(wav_bytes: bytes) -> int:
    """Peak absolute sample (16-bit PCM) for silence/debug checks."""
    import struct

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if sw != 2 or not frames:
        return 0
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    if nch == 2:
        return max(max(abs(samples[i]), abs(samples[i + 1])) for i in range(0, len(samples), 2))
    return max(abs(s) for s in samples)


def wav_to_mono_for_whisper(wav_bytes: bytes) -> bytes:
    """Downmix stereo Discord PCM to mono — improves Whisper results on VC audio."""
    import struct

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        rate = wf.getframerate()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if nch != 2 or sw != 2:
        return wav_bytes
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    mono: list[int] = []
    for i in range(0, len(samples), 2):
        mono.append((samples[i] + samples[i + 1]) // 2)
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{len(mono)}h", *mono))
    return out.getvalue()


def wav_scale_peak(wav_bytes: bytes, *, target: int = 24000) -> bytes:
    """Attenuate clipped Discord PCM (peak ~32768) before Whisper."""
    import struct

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nch = wf.getnchannels()
        rate = wf.getframerate()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if sw != 2 or not frames:
        return wav_bytes
    samples = list(struct.unpack(f"<{len(frames) // 2}h", frames))
    peak = max(abs(s) for s in samples) if samples else 0
    if peak <= target:
        return wav_bytes
    scale = target / peak
    scaled = [max(-32768, min(32767, int(s * scale))) for s in samples]
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(sw)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{len(scaled)}h", *scaled))
    return out.getvalue()


def prepare_discord_vc_pcm(pcm: bytes, *, target_peak: int = 10000) -> tuple[bytes, int]:
    """Mono-mix + peak-normalize Discord VC PCM (Opus decode is often clipped stereo)."""
    mono, peak = stereo_pcm_to_mono_s16(pcm)
    if not mono:
        return pcm, 0
    if peak > target_peak:
        import array

        samples = array.array("h")
        samples.frombytes(mono)
        scale = target_peak / peak
        mono = array.array(
            "h", (max(-32768, min(32767, int(s * scale))) for s in samples)
        ).tobytes()
        peak = max(abs(s) for s in array.array("h", mono))
    return mono, peak


def stereo_pcm_to_mono_s16(pcm: bytes) -> tuple[bytes, int]:
    """Downmix interleaved stereo s16 PCM to mono; return (mono_bytes, peak)."""
    import array

    if len(pcm) < 4:
        return b"", 0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    mono = array.array("h")
    for i in range(0, len(samples) - 1, 2):
        mono.append((samples[i] + samples[i + 1]) // 2)
    if len(samples) % 2 == 1:
        mono.append(samples[-1])
    if not mono:
        return b"", 0
    peak = max(abs(s) for s in mono)
    return mono.tobytes(), peak


def attenuate_pcm_s16(pcm: bytes, *, target_peak: int = 20000) -> bytes:
    """Scale interleaved s16 PCM down when Discord VC pegs samples at ±32768."""
    import array

    if len(pcm) < 2:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return pcm
    peak = max(abs(s) for s in samples)
    if peak <= target_peak:
        return pcm
    scale = target_peak / peak
    scaled = array.array("h", (max(-32768, min(32767, int(s * scale))) for s in samples))
    return scaled.tobytes()


def soft_limit_mono_s16(mono: bytes, *, target_peak: int = 8000) -> tuple[bytes, int]:
    """Scale mono PCM down when Discord VC pegs samples (before WAV/Whisper)."""
    import array

    if len(mono) < 2:
        return mono, 0
    samples = array.array("h")
    samples.frombytes(mono[: len(mono) - (len(mono) % 2)])
    if not samples:
        return mono, 0
    peak = max(abs(s) for s in samples)
    if peak <= target_peak:
        return mono, peak
    scale = target_peak / peak
    limited = array.array(
        "h", (max(-32768, min(32767, int(s * scale))) for s in samples)
    )
    new_peak = max(abs(s) for s in limited)
    return limited.tobytes(), new_peak


def pick_vc_wav_for_whisper(
    wav_bytes: bytes,
    *,
    raw_peak: int,
    scale_target: int = 12000,
    min_peak: int = 300,
    max_input_peak: int = 32760,
) -> tuple[bytes, int, bool]:
    """Pick the softest declip that still clears thresholds (hot VC clips need lower gain)."""
    if raw_peak >= 28000:
        targets = (6000, 8000, 10000, min(scale_target, 12000))
    elif raw_peak >= 12000:
        targets = (8000, 10000, scale_target)
    else:
        targets = (scale_target,)
    for target in targets:
        wav, peak, clipped = wav_prepare_for_whisper(wav_bytes, scale_target=target)
        if peak >= min_peak and peak < max_input_peak:
            return wav, peak, clipped
    wav, peak, clipped = wav_prepare_for_whisper(wav_bytes, scale_target=targets[-1])
    if peak >= max_input_peak:
        for softer in (8000, 6000, 4000):
            wav, peak, clipped = wav_prepare_for_whisper(wav_bytes, scale_target=softer)
            if peak < max_input_peak:
                break
    return wav, peak, clipped


def wav_prepare_for_whisper(wav_bytes: bytes, *, scale_target: int = 24000) -> tuple[bytes, int, bool]:
    """Mono + de-clipping. Returns (wav, peak, was_clipped)."""
    mono = wav_to_mono_for_whisper(wav_bytes)
    peak = wav_peak_amplitude(mono)
    clipped = peak >= 24000
    if peak >= 24000:
        mono = wav_scale_peak(mono, target=scale_target)
        peak = wav_peak_amplitude(mono)
        if peak >= 24000:
            mono = wav_scale_peak(mono, target=max(6000, scale_target // 2))
            peak = wav_peak_amplitude(mono)
    return mono, peak, clipped


def parse_discord_vc_skip_sets(sec: dict[str, Any]) -> tuple[set[int], set[str]]:
    """User IDs / display names to skip on VC decode (use LOCAL mic for yourself)."""
    skip_uids: set[int] = set()
    for x in sec.get("discord_vc_skip_user_ids") or []:
        try:
            skip_uids.add(int(x))
        except (TypeError, ValueError):
            pass
    skip_names = {
        str(x).strip().lower()
        for x in (sec.get("discord_vc_skip_display_names") or [])
        if str(x).strip()
    }
    return skip_uids, skip_names


def pcm_to_wav_format(
    pcm: bytes,
    *,
    rate: int,
    channels: int,
    sample_width: int,
) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def voice_transcribe_chunk_byte_limits(
    config: dict[str, Any],
    *,
    rate: int,
    channels: int,
    sample_width: int,
    for_discord_vc: bool = False,
) -> tuple[int, int]:
    """PCM byte thresholds for chunking (Discord VC can use shorter chunks)."""
    sec = config.get("discord_voice_transcribe") or {}
    if for_discord_vc:
        chunk_sec = float(sec.get("discord_vc_chunk_seconds", 3.0))
        min_sec = float(sec.get("discord_vc_min_chunk_seconds", 1.2))
    else:
        chunk_sec = float(sec.get("chunk_seconds", 6))
        min_sec = float(sec.get("min_chunk_seconds", 2))
    per_sec = rate * channels * sample_width
    return int(per_sec * chunk_sec), int(per_sec * min_sec)


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
