"""
OS process: consume WAV bytes from a Queue → OpenAI Whisper → Discord text channel (REST only, no gateway).

Pair with ``discord_voice_listen_process`` (separate process).
"""

from __future__ import annotations

import io
import logging
import multiprocessing
import os
import queue
import ssl
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from discord_voice_common import load_config_dict, make_openai_client

log = logging.getLogger(__name__)

_DISCORD_API = "https://discord.com/api/v10"


def _discord_rest_http_client(cfg: dict[str, Any]) -> httpx.Client:
    """HTTPS to discord.com uses OS trust store (Windows CA) when ``truststore`` is installed."""
    sec = cfg.get("discord_voice_transcribe") or {}
    if sec.get("discord_rest_tls_verify") is False:
        log.warning("[Transcribe] discord_rest_tls_verify=false — insecure HTTPS to Discord API")
        return httpx.Client(timeout=30.0, verify=False)
    try:
        import truststore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return httpx.Client(timeout=30.0, verify=ctx)
    except ImportError:
        log.warning("[Transcribe] truststore not installed — Discord REST may fail behind TLS inspection")
        return httpx.Client(timeout=30.0)


def _post_message(
    http: httpx.Client, token: str, channel_id: int, content: str
) -> None:
    t = token.strip()
    if t.startswith("Bot "):
        auth = t
    else:
        auth = f"Bot {t}"
    url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    r = http.post(
        url,
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json={"content": content},
    )
    if r.status_code >= 400:
        log.error("[Transcribe] Discord REST %s: %s", r.status_code, r.text[:500])


def transcribe_process_entry(config_path: str, in_queue: "multiprocessing.Queue") -> None:
    """multiprocessing.Process target for Whisper + posting."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
        stream=sys.stdout,
        force=True,
    )
    root = Path(config_path).resolve().parent
    os.chdir(str(root))

    cfg = load_config_dict(config_path)
    sec = cfg.get("discord_voice_transcribe") or {}
    discord_cfg = cfg.get("discord") or {}
    token = discord_cfg.get("bot_token")
    if not token:
        log.error("[Transcribe] No discord.bot_token")
        return

    try:
        out_ch_id = int(sec["output_channel_id"])
    except (KeyError, TypeError, ValueError):
        log.error("[Transcribe] output_channel_id missing")
        return

    whisper_model = str(sec.get("whisper_model", "whisper-1"))
    lang_o = sec.get("language")
    lang = str(lang_o).strip() if lang_o else None

    try:
        oai = make_openai_client(cfg)
    except Exception as e:
        log.error("[Transcribe] OpenAI client failed: %s", e)
        return

    http = _discord_rest_http_client(cfg)
    log.info("[Transcribe] Worker ready — Whisper → channel %s", out_ch_id)

    try:
        _run_transcribe_loop(in_queue, oai, http, str(token), out_ch_id, whisper_model, lang)
    finally:
        http.close()


def _run_transcribe_loop(
    in_queue: "multiprocessing.Queue",
    oai: Any,
    http: httpx.Client,
    token: str,
    out_ch_id: int,
    whisper_model: str,
    lang: str | None,
) -> None:
    while True:
        try:
            item = in_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if item is None:
            log.info("[Transcribe] Shutdown sentinel received")
            break

        try:
            _uid, display_name, wav_bytes = item
        except Exception:
            log.warning("[Transcribe] Bad queue item: %s", type(item))
            continue

        buf = io.BytesIO(wav_bytes)
        buf.name = "chunk.wav"
        try:
            kwargs: dict[str, Any] = {"model": whisper_model, "file": buf}
            if lang:
                kwargs["language"] = lang
            tr = oai.audio.transcriptions.create(**kwargs)
            text = (getattr(tr, "text", None) or "").strip()
            if text:
                lead = f"**{display_name}:** "
                rest = text
                chunk = 1900
                while rest:
                    piece = rest[: chunk - len(lead)]
                    rest = rest[len(piece) :]
                    _post_message(http, token, out_ch_id, lead + piece)
                    lead = ""
                    time.sleep(0.35)
        except Exception:
            log.exception("[Transcribe] Whisper/post failed for %s", display_name)
