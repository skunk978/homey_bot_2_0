"""
Per-speaker local transcription: Discord VC detects who is talking; Voicemeeter playback
(B1 / Discord output) is recorded only while that user is active, one buffer per user.

Requires Discord output routed to Voicemeeter VAIO → bus B1 (or configured voicemeeter_bus).
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any

import pyaudio

from discord_voice_common import (
    config_bool,
    install_ascii_console_logging,
    load_config_dict,
    pcm_to_wav_format,
    stereo_pcm_to_mono_s16,
    voice_transcribe_chunk_byte_limits,
    voicemeeter_capture_device_name,
    wav_peak_amplitude,
)
from discord_voice_local_mic import _find_input_device_index, _put_wav

log = logging.getLogger(__name__)

_SAMPLE_WIDTH = 2


class _SpeakerGateState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_uid: int | None = None
        self._buffers: dict[int, bytearray] = {}
        self._names: dict[int, str] = {}

    def on_start(self, uid: int, name: str) -> int | None:
        """Begin capturing local playback for this Discord user. Returns uid flushed, if any."""
        flushed: int | None = None
        with self._lock:
            if self._current_uid is not None and self._current_uid != uid:
                flushed = self._current_uid
            self._current_uid = uid
            self._names[uid] = name
            self._buffers[uid] = bytearray()
        return flushed

    def on_flush(self, uid: int) -> tuple[int, str, bytes] | None:
        with self._lock:
            if self._current_uid == uid:
                self._current_uid = None
            return self._take_buffer_locked(uid)

    def append_pcm(self, raw: bytes) -> None:
        with self._lock:
            uid = self._current_uid
            if uid is None:
                return
            self._buffers.setdefault(uid, bytearray()).extend(raw)

    def _take_buffer_locked(self, uid: int) -> tuple[int, str, bytes] | None:
        buf = self._buffers.pop(uid, None)
        name = self._names.get(uid, str(uid))
        if not buf:
            return None
        return uid, name, bytes(buf)

    def take_buffer(self, uid: int) -> tuple[int, str, bytes] | None:
        with self._lock:
            return self._take_buffer_locked(uid)


def run_speaker_gated_local_capture(
    config_path: str,
    out_queue: "Any",
    speaker_event_queue: "Any",
    stop_event: threading.Event,
) -> None:
    """
    multiprocessing/thread target: Voicemeeter playback → transcribe queue, gated by VC speaker.
    """
    os.chdir(str(Path(config_path).resolve().parent))
    cfg = load_config_dict(config_path)
    sec = cfg.get("discord_voice_transcribe") or {}
    capture_dev = voicemeeter_capture_device_name(sec)
    bus = str(sec.get("voicemeeter_bus") or "B1").strip().upper()
    log.info(
        "[SpeakerLocal] Per-user local transcribe: device=%r bus=%s (Discord VC → who speaks)",
        capture_dev,
        bus,
    )

    _, min_bytes = voice_transcribe_chunk_byte_limits(
        cfg, rate=48000, channels=2, sample_width=_SAMPLE_WIDTH, for_discord_vc=True
    )

    state = _SpeakerGateState()
    pa = pyaudio.PyAudio()
    dev_index = _find_input_device_index(pa, capture_dev)
    try:
        info = pa.get_device_info_by_index(dev_index)
    except Exception:
        info = pa.get_default_input_device_info()
        dev_index = int(info["index"])

    rate = int(float(info.get("defaultSampleRate") or 48000))
    max_in = int(info.get("maxInputChannels") or 0)
    channels = 2 if max_in >= 2 else 1
    frame_ms = int(sec.get("local_microphone_frame_ms", 50) or 50)
    frame_ms = max(10, min(frame_ms, 200))
    frames_per_read = max(1, int(rate * (frame_ms / 1000.0)) * channels)

    min_flush_peak = int(sec.get("discord_vc_min_peak_amplitude", 300))

    def flush_uid(uid: int, name: str, pcm_stereo: bytes) -> None:
        if len(pcm_stereo) < min_bytes:
            log.debug("[SpeakerLocal] Skip flush %s — short buffer (%d bytes)", name, len(pcm_stereo))
            return
        mono, peak = stereo_pcm_to_mono_s16(pcm_stereo)
        if not mono or peak < min_flush_peak:
            log.info(
                "[SpeakerLocal] Skip flush %s — quiet (peak=%s < %s)",
                name,
                peak,
                min_flush_peak,
            )
            return
        wav = pcm_to_wav_format(mono, rate=rate, channels=1, sample_width=_SAMPLE_WIDTH)
        pcm_sec = len(mono) / (rate * _SAMPLE_WIDTH)
        log.info(
            "[SpeakerLocal] Chunk for %s (~%.1fs local playback, peak=%s, %d WAV)",
            name,
            pcm_sec,
            peak,
            len(wav),
        )
        _put_wav(out_queue, name, wav, capture_uid=uid)

    def event_loop() -> None:
        while not stop_event.is_set():
            try:
                item = speaker_event_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                kind, uid, name = item[0], int(item[1]), str(item[2])
            except (TypeError, ValueError, IndexError):
                continue
            if kind == "start":
                prev = state.on_start(uid, name)
                if prev is not None:
                    taken = state.take_buffer(prev)
                    if taken:
                        pu, pn, pcm = taken
                        flush_uid(pu, pn, pcm)
                log.info("[SpeakerLocal] Discord reports %s speaking → capture local audio", name)
            elif kind == "flush":
                taken = state.on_flush(uid)
                if taken:
                    u, n, pcm = taken
                    flush_uid(u, n, pcm)

    ev_thread = threading.Thread(target=event_loop, name="speaker-local-events", daemon=True)
    ev_thread.start()

    stream = pa.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=rate,
        input=True,
        input_device_index=dev_index,
        frames_per_buffer=frames_per_read,
    )
    log.info(
        "[SpeakerLocal] Opened %r (rate=%s ch=%s) — waiting for VC speaker events",
        info.get("name", capture_dev),
        rate,
        channels,
    )
    try:
        while not stop_event.is_set():
            try:
                raw = stream.read(frames_per_read, exception_on_overflow=False)
            except Exception:
                log.exception("[SpeakerLocal] Capture read failed")
                break
            if raw:
                state.append_pcm(raw)
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()
        ev_thread.join(timeout=2.0)
        log.info("[SpeakerLocal] Stopped")
