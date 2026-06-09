"""
OS process: Discord voice receive only → push WAV chunks to a multiprocessing.Queue.

Started by the main bot when ``discord_voice_transcribe.enabled`` (see homey_bot_space_lord).
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import queue
import sys
import time
from pathlib import Path
from typing import Any

import discord
from discord.ext import voice_recv

from discord_voice_common import (
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    attenuate_pcm_s16,
    config_bool,
    install_ascii_console_logging,
    join_discord_voice_channel_enabled,
    load_config_dict,
    log_discord_guild_hint,
    pcm_rms_s16,
    prepare_discord_vc_pcm,
    parse_discord_vc_skip_sets,
    patch_voice_recv_robustness,
    pcm_to_wav,
    pcm_to_wav_format,
    receive_vc_audio_enabled,
    resolve_discord_channel,
    speaker_gated_local_transcribe_enabled,
    stereo_pcm_to_mono_s16,
    soft_limit_mono_s16,
    trim_pcm_speech_window,
    voice_transcribe_chunk_byte_limits,
)

log = logging.getLogger(__name__)


async def _disconnect_all_voice_sessions(client: discord.Client) -> None:
    """Drop stale voice sessions (duplicate bot / crashed run) before a new connect."""
    for guild in client.guilds:
        vc = guild.voice_client
        if vc is None:
            continue
        try:
            log.warning(
                "[Listen] Disconnecting existing voice in guild %s before connect",
                getattr(guild, "name", guild.id),
            )
            await vc.disconnect(force=True)
        except Exception:
            log.debug("[Listen] Could not disconnect guild voice", exc_info=True)
    await asyncio.sleep(2.0)


def _put_speaker_event(
    speaker_event_queue: "Any | None",
    kind: str,
    uid: int,
    display_name: str,
) -> None:
    if speaker_event_queue is None:
        return
    try:
        speaker_event_queue.put((kind, uid, display_name), timeout=1.0)
    except Exception:
        log.warning("[Listen] Speaker event queue full — drop %s for %s", kind, display_name)


async def _run_listen(
    config: dict[str, Any],
    out_queue: "multiprocessing.Queue",
    config_path: str,
    speak_queue: "multiprocessing.Queue | None" = None,
    speaker_event_queue: "Any | None" = None,
) -> None:
    sec = config.get("discord_voice_transcribe") or {}
    discord_cfg = config.get("discord") or {}
    token = discord_cfg.get("bot_token")
    if not token:
        log.error("[Listen] No discord.bot_token")
        return

    try:
        voice_ch_id = int(sec.get("voice_channel_id") or discord_cfg.get("voice_channel_id") or 0)
    except (TypeError, ValueError):
        voice_ch_id = 0
    if not voice_ch_id:
        log.error("[Listen] No voice channel id")
        return

    receive_audio = receive_vc_audio_enabled(sec)
    speaker_local = speaker_gated_local_transcribe_enabled(sec)
    speaker_local_vc_fallback = config_bool(
        sec.get("speaker_local_fallback_vc_decode"), True
    )
    if speaker_local:
        log.info(
            "[Listen] discord_transcribe_mode=speaker_local — VC detects who speaks; "
            "B1 capture per user%s",
            " + VC decode fallback if B1 is quiet" if speaker_local_vc_fallback else "",
        )
    # Presence-only VC (Voicemeeter transcribe): auto-reconnect=False avoids 4006 retry loops.
    voice_reconnect = config_bool(sec.get("voice_auto_reconnect"), receive_audio)
    join_vc = join_discord_voice_channel_enabled(sec)
    if not join_vc:
        log.info("[Listen] discord_join_voice_channel=false — not joining voice")
        return

    chunk_bytes, min_bytes = voice_transcribe_chunk_byte_limits(
        config, rate=SAMPLE_RATE, channels=CHANNELS, sample_width=SAMPLE_WIDTH,
        for_discord_vc=True,
    )
    log.info(
        "[Listen] VC chunking: flush at %.1fs, min %.1fs (%d / %d bytes)",
        chunk_bytes / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH),
        min_bytes / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH),
        chunk_bytes,
        min_bytes,
    )
    only_names = {
        str(x).strip().lower()
        for x in (sec.get("transcribe_only_discord_names") or [])
        if str(x).strip()
    }
    if only_names:
        log.info("[Listen] Only transcribing VC speakers: %s", sorted(only_names))
    skip_uids, skip_names = parse_discord_vc_skip_sets(sec)
    if skip_uids or skip_names:
        log.info(
            "[Listen] Skipping VC transcribe for user_ids=%s display_names=%s (use LOCAL mic instead)",
            sorted(skip_uids) if skip_uids else [],
            sorted(skip_names) if skip_names else [],
        )
    vc_pcm_target = int(sec.get("discord_vc_pcm_target_peak", 10000))
    vc_min_pcm_peak = int(sec.get("discord_vc_min_pcm_peak", 250))
    vc_normalize_in_listen = config_bool(sec.get("discord_vc_normalize_in_listen"), False)
    vc_rms_threshold = float(sec.get("discord_vc_rms_threshold", 120.0))
    vc_silence_flush_seconds = float(sec.get("discord_vc_silence_flush_seconds", 0.85))
    suppress_during_tts = config_bool(sec.get("suppress_vc_transcribe_during_tts"), True)
    tts_playing: list[bool] = [False]
    if receive_audio:
        patch_voice_recv_robustness()

    intents = discord.Intents.default()
    intents.guilds = True
    intents.voice_states = True

    client = discord.Client(intents=intents)
    pcm_queue: asyncio.Queue[tuple[int, str, bytes]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    voice_channel_ref: list[Any] = [None]

    buffers: dict[int, bytearray] = {}
    names: dict[int, str] = {}
    heard_users: set[int] = set()
    last_voice_at: dict[int, float] = {}
    local_gate_active: set[int] = set()

    def put_chunk(uid: int, display_name: str, wav: bytes) -> None:
        try:
            out_queue.put((uid, display_name, wav), timeout=2.0)
        except Exception:
            log.warning("[Listen] Output queue full or dead — dropping chunk for %s", display_name)

    def _should_skip_vc_user(uid: int, display_name: str) -> bool:
        if uid in skip_uids:
            return True
        if skip_names and str(display_name).strip().lower() in skip_names:
            return True
        if only_names and str(display_name).strip().lower() not in only_names:
            return True
        return False

    async def flush_user(uid: int, *, idle: bool = False, force: bool = False) -> None:
        buf = buffers.get(uid)
        if not buf or len(buf) < min_bytes:
            return
        raw = bytes(buf)
        display_name = names.get(uid, str(uid))
        if _should_skip_vc_user(uid, display_name):
            buffers.pop(uid, None)
            local_gate_active.discard(uid)
            return
        if speaker_local:
            await asyncio.to_thread(
                _put_speaker_event, speaker_event_queue, "flush", uid, display_name
            )
            if not speaker_local_vc_fallback:
                buffers.pop(uid, None)
                local_gate_active.discard(uid)
                return
            log.info(
                "[Listen] %s — also sending VC decode for transcribe (B1 fallback; "
                "route Discord output → Voicemeeter VAIO to use B1 only)",
                display_name,
            )
        _, raw_peak = stereo_pcm_to_mono_s16(raw)
        if raw_peak >= 28000:
            raw = attenuate_pcm_s16(raw, target_peak=12000)
            _, raw_peak = stereo_pcm_to_mono_s16(raw)
            trimmed = raw
        else:
            trimmed = trim_pcm_speech_window(
                raw,
                sample_rate=SAMPLE_RATE,
                channels=CHANNELS,
                sample_width=SAMPLE_WIDTH,
                rms_threshold=vc_rms_threshold,
            )
        if len(trimmed) < min_bytes:
            if force:
                trimmed = raw
            elif idle:
                log.debug(
                    "[Listen] Idle skip %s — too short after trim (%d bytes)",
                    display_name,
                    len(trimmed),
                )
                return
            else:
                trimmed = raw
        mono, prep_peak = stereo_pcm_to_mono_s16(trimmed)
        if prep_peak >= 24000:
            mono, prep_peak = soft_limit_mono_s16(mono, target_peak=8000)
        if not mono or prep_peak < vc_min_pcm_peak:
            log.info(
                "[Listen] Skip %s — quiet mono (peak=%s < %s)",
                display_name,
                prep_peak,
                vc_min_pcm_peak,
            )
            if force:
                buffers.pop(uid, None)
            return
        if vc_normalize_in_listen:
            mono, prep_peak = prepare_discord_vc_pcm(trimmed, target_peak=vc_pcm_target)
        wav = pcm_to_wav_format(
            mono, rate=SAMPLE_RATE, channels=1, sample_width=SAMPLE_WIDTH
        )
        pcm_sec = len(mono) / (SAMPLE_RATE * 1 * SAMPLE_WIDTH)
        if raw_peak >= 28000:
            log.info(
                "[Listen] %s audio heavily clipped (peak=%s) — lower Discord input volume",
                display_name,
                raw_peak,
            )
        log.info(
            "[Listen] Chunk for %s (~%.1fs mono, peak=%s, %d WAV bytes%s)",
            display_name,
            pcm_sec,
            prep_peak,
            len(wav),
            ", idle" if idle else "",
        )
        buffers.pop(uid, None)
        await asyncio.to_thread(put_chunk, uid, display_name, wav)

    async def pcm_processor() -> None:
        bot_id: int | None = None
        try:
            while True:
                try:
                    uid, display_name, pcm = await asyncio.wait_for(pcm_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if client.user:
                        bot_id = client.user.id
                    now = time.monotonic()
                    for u in list(buffers.keys()):
                        if bot_id and u == bot_id:
                            continue
                        if len(buffers[u]) < min_bytes:
                            continue
                        last = last_voice_at.get(u, 0.0)
                        if (now - last) >= vc_silence_flush_seconds:
                            await flush_user(u, idle=True)
                    continue

                if client.user:
                    bot_id = client.user.id
                if bot_id and uid == bot_id:
                    continue
                if _should_skip_vc_user(uid, display_name):
                    continue

                names[uid] = display_name
                if uid not in heard_users:
                    heard_users.add(uid)
                    log.info("[Listen] Receiving voice from %s in VC", display_name)
                if uid not in buffers:
                    buffers[uid] = bytearray()
                buffers[uid].extend(pcm)
                if pcm_rms_s16(pcm) >= vc_rms_threshold:
                    last_voice_at[uid] = time.monotonic()
                    if speaker_local and uid not in local_gate_active:
                        local_gate_active.add(uid)
                        await asyncio.to_thread(
                            _put_speaker_event,
                            speaker_event_queue,
                            "start",
                            uid,
                            display_name,
                        )

                if len(buffers[uid]) >= chunk_bytes:
                    await flush_user(uid, force=True)
        except asyncio.CancelledError:
            for u in list(buffers.keys()):
                await flush_user(u)
            raise

    async def speak_processor() -> None:
        from discord_voice_speak import generate_speech_wav, play_wav_on_voice_client

        if speak_queue is None:
            return
        log.info("[Listen] Discord VC TTS speak processor started")
        while True:
            try:
                item = await asyncio.to_thread(speak_queue.get, True, 1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                text, voice_hint = item
            except (TypeError, ValueError):
                continue
            text = str(text or "").strip()
            if not text:
                continue
            wav = await asyncio.to_thread(generate_speech_wav, text, voice_hint=str(voice_hint or "female"))
            if not wav:
                log.warning("[Listen] TTS generation failed for %r", text[:80])
                continue
            vch = voice_channel_ref[0]
            vc = vch.guild.voice_client if vch and vch.guild else None
            tts_playing[0] = True
            try:
                ok = await play_wav_on_voice_client(vc, wav)
            finally:
                tts_playing[0] = False
            if ok:
                log.info("[Listen] Spoke into VC: %r", text[:120])
            else:
                log.warning("[Listen] Could not play TTS in VC (not connected?)")

    def sink_write(user: Any, data: Any) -> None:
        if user is None or not data.pcm:
            return
        if suppress_during_tts and tts_playing[0]:
            return
        uid = int(user.id)
        try:
            disp = getattr(user, "display_name", None) or getattr(user, "name", None) or str(uid)
        except Exception:
            disp = str(uid)
        if _should_skip_vc_user(uid, str(disp)):
            return
        pcm = bytes(data.pcm)
        loop.call_soon_threadsafe(pcm_queue.put_nowait, (uid, str(disp), pcm))

    @client.event
    async def on_ready() -> None:
        try:
            log_discord_guild_hint(client, discord_cfg.get("guild_id"))
            vch = await resolve_discord_channel(client, voice_ch_id)
            if vch is None:
                log.error(
                    "[Listen] Voice channel %s unavailable — fix guild invite/permissions, then restart bot",
                    voice_ch_id,
                )
                return
            if not isinstance(vch, (discord.VoiceChannel, discord.StageChannel)):
                log.error(
                    "[Listen] Channel %s is %s, not a voice/stage channel",
                    voice_ch_id,
                    type(vch).__name__,
                )
                return
            voice_channel_ref[0] = vch
            connect_delay = float(sec.get("voice_connect_delay_seconds", 4))
            if connect_delay > 0:
                log.info(
                    "[Listen] Waiting %.1fs before voice connect (session settle; reduces 4006)",
                    connect_delay,
                )
                await asyncio.sleep(connect_delay)
            await _disconnect_all_voice_sessions(client)
            guild = vch.guild
            existing = guild.voice_client if guild else None
            if existing is not None:
                log.warning("[Listen] Disconnecting stale voice client before reconnect")
                await existing.disconnect(force=True)
                await asyncio.sleep(1.5)
            if receive_audio:
                vc = await vch.connect(
                    cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=voice_reconnect
                )
                vc.listen(voice_recv.BasicSink(sink_write, decode=True))
                log.info(
                    "[Listen] Joined voice channel %s in %s -> PCM queue (config=%s)",
                    vch.name,
                    getattr(vch.guild, "name", "?"),
                    config_path,
                )
            else:
                await vch.connect(timeout=60.0, reconnect=voice_reconnect)
                log.info(
                    "[Listen] Joined voice channel %s in %s (in VC, receive OFF — "
                    "transcribe via Voicemeeter; reconnect=%s)",
                    vch.name,
                    getattr(vch.guild, "name", "?"),
                    voice_reconnect,
                )
                if not voice_reconnect:
                    log.info(
                        "[Listen] voice_auto_reconnect=false — if you see WS 4006 once, "
                        "transcribe still works via Voicemeeter; set discord_join_voice_channel: "
                        "false to skip VC entirely."
                    )
        except Exception:
            log.exception("[Listen] Voice connect failed")

    proc = None
    speak_proc = None
    if receive_audio:
        proc = asyncio.create_task(pcm_processor(), name="discord_listen_pcm")
    if speak_queue is not None and config_bool(sec.get("tts_to_discord"), True):
        speak_proc = asyncio.create_task(speak_processor(), name="discord_vc_speak")
    try:
        await client.start(str(token).strip())
    except asyncio.CancelledError:
        raise
    finally:
        if proc is not None:
            proc.cancel()
            try:
                await proc
            except asyncio.CancelledError:
                pass
        if speak_proc is not None:
            speak_proc.cancel()
            try:
                await speak_proc
            except asyncio.CancelledError:
                pass
        if not client.is_closed():
            await client.close()


def listen_process_entry(
    config_path: str,
    out_queue: "multiprocessing.Queue",
    speak_queue: "multiprocessing.Queue | None" = None,
    speaker_event_queue: "Any | None" = None,
) -> None:
    """multiprocessing.Process target — must be top-level for Windows spawn."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        stream=sys.stdout,
        force=True,
    )
    install_ascii_console_logging()
    # RTCP SenderReport spam at INFO from voice_recv; not actionable for us.
    logging.getLogger("discord.ext.voice_recv").setLevel(logging.WARNING)
    os.chdir(str(Path(config_path).resolve().parent))
    cfg = load_config_dict(config_path)
    asyncio.run(
        _run_listen(cfg, out_queue, config_path, speak_queue, speaker_event_queue)
    )
