"""
OS process: Discord voice receive only → push WAV chunks to a multiprocessing.Queue.

Started by the main bot when ``discord_voice_transcribe.enabled`` (see homey_bot_space_lord).
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import sys
from pathlib import Path
from typing import Any

import discord
from discord.ext import voice_recv

from discord_voice_common import (
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    load_config_dict,
    patch_voice_recv_robustness,
    pcm_to_wav,
)

log = logging.getLogger(__name__)


def _chunk_limits(config: dict[str, Any]) -> tuple[int, int, int]:
    sec = config.get("discord_voice_transcribe") or {}
    chunk_sec = float(sec.get("chunk_seconds", 6))
    min_sec = float(sec.get("min_chunk_seconds", 2))
    chunk_bytes = int(SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * chunk_sec)
    min_bytes = int(SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * min_sec)
    return chunk_bytes, min_bytes


async def _run_listen(config: dict[str, Any], out_queue: "multiprocessing.Queue", config_path: str) -> None:
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

    chunk_bytes, min_bytes = _chunk_limits(config)
    patch_voice_recv_robustness()

    intents = discord.Intents.default()
    intents.guilds = True
    intents.voice_states = True

    client = discord.Client(intents=intents)
    pcm_queue: asyncio.Queue[tuple[int, str, bytes]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    buffers: dict[int, bytearray] = {}
    names: dict[int, str] = {}

    def put_chunk(uid: int, display_name: str, wav: bytes) -> None:
        try:
            out_queue.put((uid, display_name, wav), timeout=2.0)
        except Exception:
            log.warning("[Listen] Output queue full or dead — dropping chunk for %s", display_name)

    async def flush_user(uid: int) -> None:
        raw = buffers.pop(uid, None)
        if not raw or len(raw) < min_bytes:
            return
        display_name = names.get(uid, str(uid))
        wav = pcm_to_wav(bytes(raw))
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
                    for u in list(buffers.keys()):
                        if bot_id and u == bot_id:
                            continue
                        if len(buffers[u]) >= min_bytes:
                            await flush_user(u)
                    continue

                if client.user:
                    bot_id = client.user.id
                if bot_id and uid == bot_id:
                    continue

                names[uid] = display_name
                if uid not in buffers:
                    buffers[uid] = bytearray()
                buffers[uid].extend(pcm)

                if len(buffers[uid]) >= chunk_bytes:
                    await flush_user(uid)
        except asyncio.CancelledError:
            for u in list(buffers.keys()):
                await flush_user(u)
            raise

    def sink_write(user: Any, data: Any) -> None:
        if user is None or not data.pcm:
            return
        uid = int(user.id)
        try:
            disp = getattr(user, "display_name", None) or getattr(user, "name", None) or str(uid)
        except Exception:
            disp = str(uid)
        pcm = bytes(data.pcm)
        loop.call_soon_threadsafe(pcm_queue.put_nowait, (uid, str(disp), pcm))

    @client.event
    async def on_ready() -> None:
        try:
            vch = client.get_channel(voice_ch_id)
            if vch is None or not isinstance(vch, discord.VoiceChannel):
                log.error("[Listen] Voice channel %s not found", voice_ch_id)
                return
            vc = await vch.connect(cls=voice_recv.VoiceRecvClient, timeout=60.0, reconnect=True)
            vc.listen(voice_recv.BasicSink(sink_write, decode=True))
            log.info("[Listen] Connected to voice #%s → PCM queue (config=%s)", vch.name, config_path)
        except Exception:
            log.exception("[Listen] Voice connect failed")

    proc = asyncio.create_task(pcm_processor(), name="discord_listen_pcm")
    try:
        await client.start(str(token).strip())
    except asyncio.CancelledError:
        raise
    finally:
        proc.cancel()
        try:
            await proc
        except asyncio.CancelledError:
            pass
        if not client.is_closed():
            await client.close()


def listen_process_entry(config_path: str, out_queue: "multiprocessing.Queue") -> None:
    """multiprocessing.Process target — must be top-level for Windows spawn."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        stream=sys.stdout,
        force=True,
    )
    os.chdir(str(Path(config_path).resolve().parent))
    cfg = load_config_dict(config_path)
    asyncio.run(_run_listen(cfg, out_queue, config_path))
