"""

Background thread: capture a Windows recording device -> same Queue as Discord VC listen.



Used for Voicemeeter Banana: Discord plays to "Voicemeeter Input (VAIO)"; record the matching

Voicemeeter Out bus (e.g. Out A1) via ``local_microphone_device``. Whisper labels lines with

``local_microphone_label`` (e.g. "Discord"). With VC listen enabled, other speakers still get

Discord nicknames from the bot in voice channel.



Laptop physical mics (while Discord uses the same device) use Windows WASAPI *shared* mode via

``sounddevice`` — see ``laptop_microphone_use_wasapi_shared`` in config.

"""



from __future__ import annotations



import array

import logging

import multiprocessing

import os

import sys

import threading

import time

from pathlib import Path

from typing import Any, Callable



import pyaudio



from discord_voice_common import (

    config_bool,

    load_config_dict,

    pcm_to_wav_format,

    voice_transcribe_chunk_byte_limits,

    voicemeeter_capture_device_name,

    wav_peak_amplitude,

)



log = logging.getLogger(__name__)



PA_FORMAT = pyaudio.paInt16

_SAMPLE_WIDTH = 2





def _pcm_rms_s16(pcm: bytes) -> float:

    if len(pcm) < 2:

        return 0.0

    a = array.array("h")

    a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])

    if not a:

        return 0.0

    return (sum(x * x for x in a) / len(a)) ** 0.5





def _voicemeeter_bus_from_device_name(device_name: str) -> str | None:

    import re



    m = re.search(r"out\s+([ab]\d+)", device_name, re.IGNORECASE)

    return m.group(1).upper() if m else None





def _find_input_device_index(pa: pyaudio.PyAudio, substring: str | None) -> int:

    env = os.environ.get("HOMEY_LOCAL_MIC_DEVICE", "").strip()

    sub = (substring or "").strip() or env or ""

    required_bus = _voicemeeter_bus_from_device_name(sub) if sub else None

    if sub:

        needle = sub.lower()

        matches: list[tuple[int, str, int]] = []

        for i in range(pa.get_device_count()):

            info = pa.get_device_info_by_index(i)

            if int(info.get("maxInputChannels") or 0) < 1:

                continue

            name = str(info.get("name", ""))

            lower = name.lower()

            if needle not in lower:

                continue

            if required_bus:

                bus_token = f"out {required_bus.lower()}"

                if bus_token not in lower:

                    continue

                if required_bus == "B1" and "out a1" in lower:

                    continue

                if required_bus == "A1" and "out b1" in lower:

                    continue

            score = len(name)

            if "vaio" in lower:

                score += 100

            if required_bus and f"out {required_bus.lower()}" in lower:

                score += 200

            matches.append((score, name, i))

        if matches:

            matches.sort(reverse=True)

            _score, best_name, best_i = matches[0]

            log.info("[LocalMic] Using input device #%s: %s", best_i, best_name)

            return best_i

        log.warning("[LocalMic] No input device name contains %r — using system default", sub)

    d = pa.get_default_input_device_info()

    idx = int(d["index"])

    log.info("[LocalMic] Using default input device #%s: %s", idx, d.get("name"))

    return idx





def _wasapi_hostapi_ids() -> set[int]:

    try:

        import sounddevice as sd

    except ImportError:

        return set()

    out: set[int] = set()

    for i, api in enumerate(sd.query_hostapis()):

        if "wasapi" in str(api.get("name", "")).lower():

            out.add(i)

    return out





def _find_sounddevice_input_index(substring: str) -> tuple[int, str, float, int]:

    import sounddevice as sd



    needle = substring.strip().lower()

    wasapi = _wasapi_hostapi_ids()

    matches: list[tuple[int, int, str, float, int]] = []

    for i, dev in enumerate(sd.query_devices()):

        if int(dev.get("max_input_channels") or 0) < 1:

            continue

        name = str(dev.get("name", ""))

        if needle not in name.lower():

            continue

        score = len(name)

        if int(dev.get("hostapi", -1)) in wasapi:

            score += 500

        rate = float(dev.get("default_samplerate") or 48000)

        max_ch = int(dev.get("max_input_channels") or 1)

        matches.append((score, i, name, rate, max_ch))

    if not matches:

        raise LookupError(f"No sounddevice input matches {substring!r}")

    matches.sort(reverse=True)

    _score, idx, name, rate, max_ch = matches[0]

    return idx, name, rate, max_ch





def _run_pcm_capture_loop(

    *,

    stop_event: threading.Event,

    read_pcm: Callable[[int], bytes | None],

    rate: int,

    channels: int,

    cfg: dict[str, Any],

    sec: dict[str, Any],

    label: str,

    capture_dev: str,

    out_queue: "multiprocessing.Queue",

    rms_threshold: float,

    opened_name: str,

    backend: str,

    vm_bus: str,

    device_substring: str | None,

    capture_uid: int = -1,

) -> None:

    chunk_bytes, min_bytes = voice_transcribe_chunk_byte_limits(

        cfg, rate=rate, channels=channels, sample_width=_SAMPLE_WIDTH

    )

    frame_ms = int(sec.get("local_microphone_frame_ms", 50) or 50)

    frame_ms = max(10, min(frame_ms, 200))

    frames_per_read = max(1, int(rate * (frame_ms / 1000.0)))



    log.info(

        "[LocalMic] Recording %s → transcribe (%s, device=%r, label=%r, rate=%s, ch=%s)",

        f"bus {vm_bus}" if not device_substring else "direct mic",

        backend,

        opened_name,

        label,

        rate,

        channels,

    )



    buf = bytearray()

    last_voice = time.monotonic()

    debug_peaks = bool(sec.get("voicemeeter_debug_peaks", False))

    last_peak_log = 0.0



    while not stop_event.is_set():

        raw = read_pcm(frames_per_read)

        if raw is None:

            break

        if capture_uid == -2:
            try:
                from local_mic_control import is_local_mic_muted

                if is_local_mic_muted():
                    buf.clear()
                    continue
            except ImportError:
                pass

        now = time.monotonic()

        rms = _pcm_rms_s16(raw)

        if rms >= rms_threshold:

            last_voice = now

        if debug_peaks and (now - last_peak_log) >= 30.0:
            last_peak_log = now
            log.debug(
                "[LocalMic] %s level (%s): rms=%.0f (need >= %s to record)",
                label,
                capture_dev,
                rms,
                rms_threshold,
            )



        if not buf and rms < rms_threshold:

            continue



        buf.extend(raw)



        if len(buf) >= chunk_bytes:

            wav = pcm_to_wav_format(bytes(buf), rate=rate, channels=channels, sample_width=_SAMPLE_WIDTH)

            pk = wav_peak_amplitude(wav)

            log.info("[LocalMic] Full chunk for %s (%d WAV bytes, peak=%s)", label, len(wav), pk)

            _put_wav(out_queue, label, wav, capture_uid=capture_uid)

            buf.clear()

        elif (now - last_voice) >= 1.0 and len(buf) >= min_bytes:
            wav = pcm_to_wav_format(bytes(buf), rate=rate, channels=channels, sample_width=_SAMPLE_WIDTH)
            pk = wav_peak_amplitude(wav)
            min_flush_peak = int(sec.get("local_microphone_min_flush_peak", 400))
            if capture_uid == -2:
                min_flush_peak = int(sec.get("local_mic_min_flush_peak", min_flush_peak))
            if pk < min_flush_peak:
                buf.clear()
                continue
            log.info("[LocalMic] Idle flush for %s (%d WAV bytes, peak=%s)", label, len(wav), pk)
            _put_wav(out_queue, label, wav, capture_uid=capture_uid)
            buf.clear()





def _run_wasapi_shared_capture(

    *,

    config_path: str,

    out_queue: "multiprocessing.Queue",

    stop_event: threading.Event,

    capture_dev: str,

    label: str,

    sec: dict[str, Any],

    cfg: dict[str, Any],

    device_substring: str | None,

    vm_bus: str,

    capture_uid: int = -1,

) -> bool:

    """Capture via WASAPI shared mode (mic usable while Discord holds the device). Returns True if running."""

    if sys.platform != "win32":

        return False

    try:

        import sounddevice as sd

    except ImportError:

        log.warning("[LocalMic] sounddevice not installed — cannot use WASAPI shared capture")

        return False



    try:

        dev_index, opened_name, default_rate, max_in = _find_sounddevice_input_index(capture_dev)

    except LookupError as e:

        log.warning("[LocalMic] WASAPI device lookup failed: %s", e)

        return False



    ch_cfg = sec.get("local_microphone_channels")

    if ch_cfg == 1 or str(ch_cfg).strip() == "1":

        channels = 1

    elif ch_cfg == 2 or str(ch_cfg).strip() == "2":

        channels = 2

    else:

        channels = 2 if max_in >= 2 else 1



    rate = int(default_rate or 48000)

    frame_ms = int(sec.get("local_microphone_frame_ms", 50) or 50)

    frame_ms = max(10, min(frame_ms, 200))

    frames_per_read = max(1, int(rate * (frame_ms / 1000.0)))

    rms_threshold = float(sec.get("local_microphone_rms_threshold", 400.0))



    extra = sd.WasapiSettings(exclusive=False)

    stream = None

    try:

        stream = sd.InputStream(

            device=dev_index,

            channels=channels,

            samplerate=rate,

            dtype="int16",

            blocksize=frames_per_read,

            extra_settings=extra,

        )

        stream.start()

    except Exception:

        if channels == 2:

            log.warning("[LocalMic] WASAPI stereo open failed; retrying mono")

            channels = 1

            try:

                stream = sd.InputStream(

                    device=dev_index,

                    channels=channels,

                    samplerate=rate,

                    dtype="int16",

                    blocksize=frames_per_read,

                    extra_settings=extra,

                )

                stream.start()

            except Exception:

                log.exception("[LocalMic] WASAPI shared capture failed to open")

                return False

        else:

            log.exception("[LocalMic] WASAPI shared capture failed to open")

            return False



    log.info(

        "[LocalMic] WASAPI shared input #%s: %s (exclusive=False; works alongside Discord)",

        dev_index,

        opened_name,

    )



    def read_pcm(n_frames: int) -> bytes | None:

        try:

            data, _overflowed = stream.read(n_frames)

            return data.tobytes()

        except Exception:

            log.exception("[LocalMic] WASAPI read failed")

            return None



    try:

        _run_pcm_capture_loop(

            stop_event=stop_event,

            read_pcm=read_pcm,

            rate=rate,

            channels=channels,

            cfg=cfg,

            sec=sec,

            label=label,

            capture_dev=capture_dev,

            out_queue=out_queue,

            rms_threshold=rms_threshold,

            opened_name=opened_name,

            backend="WASAPI shared",

            vm_bus=vm_bus,

            device_substring=device_substring,

            capture_uid=capture_uid,

        )

        return True

    finally:

        try:

            if stream is not None:

                stream.stop()

                stream.close()

        except Exception:

            pass

        log.info("[LocalMic] Stopped (%s)", label)





def _put_wav(
    out_queue: "multiprocessing.Queue",
    label: str,
    wav: bytes,
    *,
    capture_uid: int = -1,
) -> None:

    try:

        out_queue.put((capture_uid, label, wav), timeout=2.0)

    except Exception:

        log.warning("[LocalMic] Output queue full — dropping chunk for %s", label)





def run_local_mic_to_queue(

    config_path: str,

    out_queue: "multiprocessing.Queue",

    stop_event: threading.Event,

    *,

    device_substring: str | None = None,

    label_override: str | None = None,

    capture_uid: int = -1,

) -> None:

    os.chdir(str(Path(config_path).resolve().parent))

    cfg = load_config_dict(config_path)

    sec = cfg.get("discord_voice_transcribe") or {}



    label = (label_override or sec.get("local_microphone_label") or "Host").strip() or "Host"

    rms_threshold = float(sec.get("local_microphone_rms_threshold", 400.0))

    frame_ms = int(sec.get("local_microphone_frame_ms", 50) or 50)

    frame_ms = max(10, min(frame_ms, 200))



    capture_dev = (device_substring or "").strip() or voicemeeter_capture_device_name(sec)

    vm_bus = (

        str(sec.get("voicemeeter_bus") or "").strip().upper()

        or _voicemeeter_bus_from_device_name(capture_dev)

        or "?"

    )

    if device_substring:

        log.info(

            "[LocalMic] Extra capture label=%r device=%r (config=%s)",

            label,

            capture_dev,

            config_path,

        )

    else:

        log.info(

            "[LocalMic] Config voicemeeter_bus=%r capture_device=%r (config=%s)",

            sec.get("voicemeeter_bus"),

            capture_dev,

            config_path,

        )



    is_vm_out = "voicemeeter out" in capture_dev.lower()
    use_wasapi = (
        bool(device_substring)
        and not is_vm_out
        and config_bool(sec.get("laptop_microphone_use_wasapi_shared"), True)
    )

    if use_wasapi and _run_wasapi_shared_capture(

        config_path=config_path,

        out_queue=out_queue,

        stop_event=stop_event,

        capture_dev=capture_dev,

        label=label,

        sec=sec,

        cfg=cfg,

        device_substring=device_substring,

        vm_bus=vm_bus,

    ):

        return



    pa = pyaudio.PyAudio()

    dev_index = _find_input_device_index(pa, capture_dev)



    try:

        info = pa.get_device_info_by_index(dev_index)

    except Exception:

        info = pa.get_default_input_device_info()

        dev_index = int(info["index"])



    rate = int(float(info.get("defaultSampleRate") or 48000))

    max_in = int(info.get("maxInputChannels") or 0)

    ch_cfg = sec.get("local_microphone_channels")

    if ch_cfg == 1 or str(ch_cfg).strip() == "1":

        channels = 1

    elif ch_cfg == 2 or str(ch_cfg).strip() == "2":

        channels = 2

    else:

        channels = 2 if max_in >= 2 else 1



    frames_per_read = max(1, int(rate * (frame_ms / 1000.0)))



    stream = None

    try:

        stream = pa.open(

            format=PA_FORMAT,

            channels=channels,

            rate=rate,

            input=True,

            input_device_index=dev_index,

            frames_per_buffer=frames_per_read,

        )

    except Exception:

        if channels == 2:

            log.warning("[LocalMic] Stereo open failed; retrying mono")

            channels = 1

            try:

                stream = pa.open(

                    format=PA_FORMAT,

                    channels=channels,

                    rate=rate,

                    input=True,

                    input_device_index=dev_index,

                    frames_per_buffer=frames_per_read,

                )

            except Exception:

                log.exception("[LocalMic] Failed to open input stream")

                pa.terminate()

                if use_wasapi:

                    return

                if device_substring and sys.platform == "win32":

                    log.info("[LocalMic] Retrying laptop capture with WASAPI shared mode")

                    _run_wasapi_shared_capture(

                        config_path=config_path,

                        out_queue=out_queue,

                        stop_event=stop_event,

                        capture_dev=capture_dev,

                        label=label,

                        sec=sec,

                        cfg=cfg,

                        device_substring=device_substring,

                        vm_bus=vm_bus,

                    )

                return

        else:

            log.exception("[LocalMic] Failed to open input stream")

            pa.terminate()

            if device_substring and sys.platform == "win32" and not use_wasapi:

                log.info("[LocalMic] Retrying laptop capture with WASAPI shared mode")

                _run_wasapi_shared_capture(

                    config_path=config_path,

                    out_queue=out_queue,

                    stop_event=stop_event,

                    capture_dev=capture_dev,

                    label=label,

                    sec=sec,

                    cfg=cfg,

                    device_substring=device_substring,

                    vm_bus=vm_bus,

                    capture_uid=capture_uid,

                )

            return



    opened_name = str(info.get("name", ""))



    def read_pcm(n_frames: int) -> bytes | None:

        try:

            return stream.read(n_frames, exception_on_overflow=False)

        except Exception:

            log.exception("[LocalMic] read failed")

            return None



    try:

        _run_pcm_capture_loop(

            stop_event=stop_event,

            read_pcm=read_pcm,

            rate=rate,

            channels=channels,

            cfg=cfg,

            sec=sec,

            label=label,

            capture_dev=capture_dev,

            out_queue=out_queue,

            rms_threshold=rms_threshold,

            opened_name=opened_name,

            backend="PyAudio",

            vm_bus=vm_bus,

            device_substring=device_substring,

            capture_uid=capture_uid,

        )

    finally:

        try:

            if stream is not None:

                stream.stop_stream()

                stream.close()

        except Exception:

            pass

        try:

            pa.terminate()

        except Exception:

            pass

        log.info("[LocalMic] Stopped")


