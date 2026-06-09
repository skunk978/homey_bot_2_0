"""
Detect Windows default playback and recording devices before Voicemeeter / mic capture.

Used so the bot follows the laptop's current headset or speakers instead of a stale
device name in config (e.g. Microphone Array).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from discord_voice_common import (
    config_bool,
    voicemeeter_capture_device_name,
    voicemeeter_mic_capture_device_name,
    voicemeeter_out_device_for_bus,
)

log = logging.getLogger(__name__)


@dataclass
class DetectedAudioDevices:
    input_name: str = ""
    output_name: str = ""
    input_index: int | None = None
    output_index: int | None = None
    comms_input_name: str = ""
    comms_output_name: str = ""

    def physical_input_name(self) -> str:
        """Best guess at the real mic (not Voicemeeter Out / Voicemod virtual)."""
        for name in (self.comms_input_name, self.input_name):
            if name and not is_voicemeeter_device(name) and not is_virtual_mic_device(name):
                return name
        better = find_best_physical_input()
        return better or self.input_name

    def voicemeeter_input_out_bus(self) -> str | None:
        for name in (self.comms_input_name, self.input_name):
            bus = parse_voicemeeter_out_bus(name)
            if bus:
                return bus
        return None


@dataclass
class AudioCapturePlan:
    """Resolved devices to open for transcribe capture threads."""

    playback_device: str
    playback_label: str
    mic_device: str
    mic_label: str
    mic_is_voicemeeter_out: bool
    local_mic_device: str
    local_mic_is_voicemeeter_out: bool
    local_mic_capture_mode: str
    detected: DetectedAudioDevices
    warnings: list[str] = field(default_factory=list)


def is_voicemeeter_device(name: str) -> bool:
    return "voicemeeter" in (name or "").lower()


def is_virtual_mic_device(name: str) -> bool:
    """Skip Voicemod/CABLE/loopback when picking a physical headset mic."""
    n = (name or "").lower()
    skip = (
        "voicemod",
        "virtual",
        "vb-audio",
        "cable",
        "loopback",
        "stereo mix",
        "what u hear",
        "wave out",
        "output",
    )
    return any(s in n for s in skip)


def find_best_physical_input(substring: str | None = None) -> str:
    """Prefer a real headset mic over Voicemod/virtual default input."""
    try:
        import pyaudio

        pa = pyaudio.PyAudio()
        candidates: list[tuple[int, str]] = []
        try:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if int(info.get("maxInputChannels") or 0) < 1:
                    continue
                name = str(info.get("name", "") or "")
                if is_voicemeeter_device(name) or is_virtual_mic_device(name):
                    continue
                if substring and substring.lower() not in name.lower():
                    continue
                score = 0
                low = name.lower()
                if "microphone" in low or "mic" in low:
                    score += 2
                if "headset" in low or "headphone" in low or "razer" in low or "usb" in low:
                    score += 3
                candidates.append((score, name))
        finally:
            pa.terminate()
        if not candidates:
            return ""
        candidates.sort(reverse=True)
        return candidates[0][1]
    except Exception:
        log.debug("[AudioDetect] find_best_physical_input failed", exc_info=True)
        return ""


def parse_voicemeeter_out_bus(device_name: str) -> str | None:
    m = re.search(r"voicemeeter\s+out\s+([ab]\d+)", device_name or "", re.IGNORECASE)
    return m.group(1).upper() if m else None


def detect_default_audio_devices() -> DetectedAudioDevices:
    """Read OS default input/output (and communications devices when available)."""
    detected = DetectedAudioDevices()
    try:
        import pyaudio

        pa = pyaudio.PyAudio()
        try:
            inp = pa.get_default_input_device_info()
            out = pa.get_default_output_device_info()
            detected.input_name = str(inp.get("name", "") or "")
            detected.output_name = str(out.get("name", "") or "")
            detected.input_index = int(inp.get("index", -1))
            detected.output_index = int(out.get("index", -1))
        finally:
            pa.terminate()
    except Exception:
        log.exception("[AudioDetect] PyAudio default device query failed")

    try:
        import sounddevice as sd

        defs = sd.default.device
        if isinstance(defs, (list, tuple)) and len(defs) >= 2:
            in_idx, out_idx = defs[0], defs[1]
            if in_idx is not None and int(in_idx) >= 0:
                info = sd.query_devices(int(in_idx), "input")
                detected.comms_input_name = str(info.get("name", "") or "")
            if out_idx is not None and int(out_idx) >= 0:
                info = sd.query_devices(int(out_idx), "output")
                detected.comms_output_name = str(info.get("name", "") or "")
    except ImportError:
        pass
    except Exception:
        log.debug("[AudioDetect] sounddevice default query failed", exc_info=True)

    return detected


def log_detected_devices(detected: DetectedAudioDevices) -> None:
    log.info(
        "[AudioDetect] Windows default INPUT (mic): %r%s",
        detected.input_name or "?",
        f" [index {detected.input_index}]" if detected.input_index is not None else "",
    )
    log.info(
        "[AudioDetect] Windows default OUTPUT (speakers/headset): %r%s",
        detected.output_name or "?",
        f" [index {detected.output_index}]" if detected.output_index is not None else "",
    )
    if detected.comms_input_name and detected.comms_input_name != detected.input_name:
        log.info("[AudioDetect] Communications INPUT: %r", detected.comms_input_name)
    if detected.comms_output_name and detected.comms_output_name != detected.output_name:
        log.info("[AudioDetect] Communications OUTPUT: %r", detected.comms_output_name)
    phys = detected.physical_input_name()
    if phys and phys != detected.input_name:
        log.info("[AudioDetect] Physical mic (for Voicemeeter HW strip): %r", phys)
    vm_bus = detected.voicemeeter_input_out_bus()
    if vm_bus:
        log.info(
            "[AudioDetect] Default mic is Voicemeeter Out %s — Discord likely uses this bus",
            vm_bus,
        )


def resolve_capture_plan(sec: dict[str, Any]) -> AudioCapturePlan:
    """
    Decide which devices to record before starting capture threads.

    Honors config overrides; when ``auto_detect_audio_devices`` is true (default),
    aligns mic capture with the OS default input and logs headset/speaker changes.
    """
    auto = config_bool(sec.get("auto_detect_audio_devices"), True)
    detected = detect_default_audio_devices() if auto else DetectedAudioDevices()
    if auto:
        log_detected_devices(detected)

    playback_device = voicemeeter_capture_device_name(sec)
    playback_label = str(sec.get("local_microphone_label") or "Discord").strip() or "Discord"
    mic_label = (
        str(sec.get("laptop_microphone_label") or sec.get("voicemeeter_mic_label") or "Laptop").strip()
        or "Laptop"
    )
    warnings: list[str] = []

    configured_mic_bus = str(sec.get("voicemeeter_mic_bus") or "").strip().upper()
    configured_mic_dev = voicemeeter_mic_capture_device_name(sec) or str(
        sec.get("laptop_microphone_device") or ""
    ).strip()

    mic_device = ""
    mic_is_vm_out = False

    if configured_mic_dev and not auto:
        mic_device = configured_mic_dev
        mic_is_vm_out = "voicemeeter out" in mic_device.lower()
    elif configured_mic_bus:
        mic_device = voicemeeter_out_device_for_bus(configured_mic_bus)
        mic_is_vm_out = True
        if auto:
            default_bus = detected.voicemeeter_input_out_bus()
            if default_bus and default_bus != configured_mic_bus:
                warnings.append(
                    f"Windows default mic is Voicemeeter Out {default_bus} but config "
                    f"voicemeeter_mic_bus is {configured_mic_bus} — match Discord input to config "
                    f"or update voicemeeter_mic_bus."
                )
    elif auto:
        default_bus = detected.voicemeeter_input_out_bus()
        if default_bus:
            mic_device = voicemeeter_out_device_for_bus(default_bus)
            mic_is_vm_out = True
            log.info(
                "[AudioDetect] Using default Voicemeeter mic bus %s → capture %r",
                default_bus,
                mic_device,
            )
        else:
            mic_device = detected.physical_input_name() or detected.input_name
            mic_is_vm_out = False
            log.info("[AudioDetect] Using default physical mic → capture %r", mic_device)
    elif configured_mic_dev:
        mic_device = configured_mic_dev
        mic_is_vm_out = "voicemeeter out" in mic_device.lower()

    if auto and mic_is_vm_out:
        phys = detected.physical_input_name()
        if phys and not is_voicemeeter_device(phys):
            log.info(
                "[AudioDetect] Set Voicemeeter Hardware Input to %r (current Windows mic)",
                phys,
            )
            if "headset" in phys.lower() or "headphone" in phys.lower():
                log.info(
                    "[AudioDetect] Headset detected — route HW strip → %s only; keep VAIO off mic bus",
                    configured_mic_bus or detected.voicemeeter_input_out_bus() or "B2",
                )

    if auto and not is_voicemeeter_device(detected.output_name):
        if "voicemeeter" in playback_device.lower():
            warnings.append(
                f"Windows default OUTPUT is {detected.output_name!r} (not Voicemeeter). "
                "For remote audio on bus "
                f"{str(sec.get('voicemeeter_bus') or 'B1').upper()}, set Discord output to "
                "Voicemeeter Input (VAIO)."
            )

    if not mic_device:
        mic_device = configured_mic_dev or voicemeeter_out_device_for_bus("B2")
        mic_is_vm_out = True

    local_mode = str(sec.get("local_mic_capture") or "auto").strip().lower()
    phys = detected.physical_input_name() if auto else ""
    configured_phys = str(sec.get("laptop_microphone_device") or "").strip()
    if configured_phys:
        phys = configured_phys
    local_mic_device = ""
    local_mic_is_vm = False

    if local_mode in ("physical", "direct", "wasapi", "hardware"):
        local_mic_device = phys or detected.input_name or mic_device
        local_mic_is_vm = is_voicemeeter_device(local_mic_device)
        log.info(
            "[AudioDetect] LOCAL mic capture=physical → %r (WASAPI when not Voicemeeter)",
            local_mic_device,
        )
    elif local_mode in ("voicemeeter", "vm", "bus"):
        local_mic_device = mic_device
        local_mic_is_vm = mic_is_vm_out
        log.info("[AudioDetect] LOCAL mic capture=voicemeeter → %r", local_mic_device)
    else:
        if phys and not is_voicemeeter_device(phys):
            local_mic_device = phys
            local_mic_is_vm = False
            log.info(
                "[AudioDetect] LOCAL mic capture=auto (physical) → %r",
                local_mic_device,
            )
        else:
            local_mic_device = mic_device
            local_mic_is_vm = mic_is_vm_out
            log.info(
                "[AudioDetect] LOCAL mic capture=auto (voicemeeter bus) → %r",
                local_mic_device,
            )

    return AudioCapturePlan(
        playback_device=playback_device,
        playback_label=playback_label,
        mic_device=mic_device,
        mic_label=mic_label,
        mic_is_voicemeeter_out=mic_is_vm_out,
        local_mic_device=local_mic_device,
        local_mic_is_voicemeeter_out=local_mic_is_vm,
        local_mic_capture_mode=local_mode,
        detected=detected,
        warnings=warnings,
    )
