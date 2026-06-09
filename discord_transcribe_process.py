"""

OS process: consume WAV bytes from a Queue → OpenAI Whisper → terminal + optional GUI.



Discord text posting is optional (``post_transcripts_to_discord`` in config).

Pair with ``discord_voice_listen_process`` (separate process).

"""



from __future__ import annotations



import io
import logging
import multiprocessing
import os
import queue
import re
import ssl
import sys
import time
import wave
from pathlib import Path
from typing import Any



import httpx



from discord_voice_common import (
    config_bool,
    discord_snowflake_id,
    install_ascii_console_logging,
    load_config_dict,
    make_openai_client,
    pick_vc_wav_for_whisper,
    wav_peak_amplitude,
    wav_prepare_for_whisper,
)



log = logging.getLogger(__name__)



_DISCORD_API = "https://discord.com/api/v10"



# Common Whisper hallucinations on noise/silence (lowercase match).

_WHISPER_HALLUCINATION_PHRASES = (

    "thanks for watching",

    "thank you for watching",

    "please subscribe",

    "subtitles by",

    "subtitle by",

    "amara.org",

    "www.",

    "http://",

    "https://",

    "copyright",

    "all rights reserved",

    "mbc",
    "thanks for listening",
    "thank you for listening",
    "silence",
    "inaudible",
    "foreign speech",
    "foreign language",
    "beep",
    "applause",
    "laughter",
    "music playing",
    "♪",
    "look after the plants",
    "lovely day",
    "enjoyed this video",
    "like and subscribe",
    "subscribe to our channel",
    "don't forget to like",
    "thank you for joining",
    "see you next time",
    "goodbye everyone",
    "peppa pig",
    "peppa",
    "reading rainbow",
    "kids cartoon",
    "cartoon network",
    "nick jr",
    "disney junior",
    "opening theme",
    "theme song",
    "intro song",
    "otter.ai",
    "transcribed by",
)

_COUNT_WORDS = frozenset(
    {
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
        "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
    }
)

# Long counting prompts get echoed by Whisper/GPT on garbled Discord VC — do not use on API calls.
_VC_COUNTING_WHISPER_PROMPT = "1 2 3 4 5 6 7 8 9 10"
_VC_COUNTING_GPT_PROMPT = "One, two, three, four, five, six, seven, eight, nine, ten."





def _discord_rest_http_client(cfg: dict[str, Any]) -> httpx.Client:

    """HTTPS to discord.com uses OS trust store (Windows CA) when ``truststore`` is installed."""

    sec = cfg.get("discord_voice_transcribe") or {}

    if sec.get("discord_rest_tls_verify") is False:

        log.warning("[Transcribe] discord_rest_tls_verify=false - insecure HTTPS to Discord API")

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





def _transcribe_settings(sec: dict[str, Any]) -> dict[str, Any]:

    """Read anti-hallucination / Whisper options from config."""

    min_peak = int(sec.get("min_peak_amplitude", 1500))

    temp = sec.get("whisper_temperature", 0)

    try:

        temperature = float(temp)

    except (TypeError, ValueError):

        temperature = 0.0

    temperature = max(0.0, min(1.0, temperature))

    lang_o = sec.get("language")

    lang = str(lang_o).strip() if lang_o else None

    max_no_speech = float(sec.get("max_no_speech_prob", 0.85))

    max_compression = float(sec.get("max_compression_ratio", 2.35))
    max_input_peak = int(sec.get("max_input_peak_before_skip", 31800))
    local_min_peak = int(sec.get("local_min_peak_amplitude", min(800, min_peak)))
    local_max_input_peak = int(sec.get("local_max_input_peak_before_skip", 32760))
    local_scale_target = int(sec.get("local_wav_scale_target", 20000))
    vc_min_peak = int(sec.get("discord_vc_min_peak_amplitude", 600))
    vc_max_input_peak = int(sec.get("discord_vc_max_input_peak_before_skip", 32760))
    vc_scale_target = int(sec.get("discord_vc_wav_scale_target", 16000))
    vc_max_no_speech = float(sec.get("discord_vc_max_no_speech_prob", 0.45))
    vc_max_compression = float(sec.get("discord_vc_max_compression_ratio", 2.0))
    local_max_no_speech = float(sec.get("local_max_no_speech_prob", 0.32))
    local_max_compression = float(sec.get("local_max_compression_ratio", 1.75))
    min_avg_logprob = float(sec.get("whisper_min_avg_logprob", -0.7))
    vc_min_avg_logprob = float(sec.get("discord_vc_min_avg_logprob", -1.0))
    whisper_prompt = str(sec.get("whisper_prompt") or "").strip() or (
        "Plain spoken English. Short phrases and counting numbers only. "
        "Transcribe exactly what is said, nothing more."
    )
    vc_whisper_prompt = str(sec.get("discord_vc_whisper_prompt") or "").strip()
    mirror_vc_activity = config_bool(sec.get("mirror_vc_activity_to_gui"), False)

    return {

        "min_peak": min_peak,

        "temperature": temperature,

        "lang": lang,

        "max_no_speech": max_no_speech,

        "max_compression": max_compression,

        "max_input_peak": max_input_peak,

        "local_min_peak": local_min_peak,

        "local_max_input_peak": local_max_input_peak,

        "local_scale_target": local_scale_target,

        "vc_min_peak": vc_min_peak,

        "vc_max_input_peak": vc_max_input_peak,

        "vc_scale_target": vc_scale_target,

        "vc_max_no_speech": vc_max_no_speech,

        "vc_max_compression": vc_max_compression,

        "local_max_no_speech": local_max_no_speech,

        "local_max_compression": local_max_compression,

        "min_avg_logprob": min_avg_logprob,

        "vc_min_avg_logprob": vc_min_avg_logprob,

        "whisper_prompt": whisper_prompt,

        "vc_whisper_prompt": vc_whisper_prompt,

        "mirror_vc_activity": mirror_vc_activity,

    }





def _wav_duration_seconds(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = wf.getframerate() or 1
            return wf.getnframes() / float(rate)
    except Exception:
        return 0.0


def _is_counting_or_numbers(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if re.fullmatch(r"[\d\s,\.\-]+", t):
        return True
    words = [w.strip(".,!?") for w in t.split() if w.strip(".,!?")]
    if not words:
        return False
    return all(w.lower() in _COUNT_WORDS or w.isdigit() for w in words)


def _extract_first_counting_run(text: str) -> list[str]:
    """First contiguous run of spoken numbers (digits or count words)."""
    parts: list[str] = []
    for tok in re.findall(r"\d+|[a-zA-Z']+", (text or "").lower()):
        clean = tok.strip("'")
        if clean.isdigit() or clean in _COUNT_WORDS:
            parts.append(clean)
        elif parts:
            break
    return parts


def _dedupe_repeated_count_run(parts: list[str]) -> list[str]:
    """If Whisper repeats '1 2 3 1 2 3', keep one cycle."""
    n = len(parts)
    for size in range(1, n // 2 + 1):
        if parts[:size] == parts[size : 2 * size]:
            return parts[:size]
    return parts


def _speech_duration_from_transcription(tr: Any) -> float | None:
    segments = getattr(tr, "segments", None)
    if not segments:
        return None
    try:
        starts = [float(getattr(s, "start", 0) or 0) for s in segments]
        ends = [float(getattr(s, "end", 0) or 0) for s in segments]
        if not ends:
            return None
        span = max(ends) - min(starts)
        return span if span > 0.15 else None
    except (TypeError, ValueError):
        return None


def _cap_speech_seconds(duration_sec: float, speech_sec: float | None) -> float | None:
    """Whisper segment spans can exceed chunk length on VC hallucinations."""
    if not speech_sec or speech_sec <= 0.2 or duration_sec <= 0:
        return speech_sec
    return min(float(speech_sec), max(0.35, duration_sec * 1.1))


def _counting_max_tokens(duration_sec: float, *, vc: bool = False) -> int:
    """Cap digit runs so short VC clips (e.g. '1 2 3') don't keep prompt-echo padding."""
    if vc:
        if duration_sec <= 1.2:
            return 3
        if duration_sec <= 2.5:
            return 4
        if duration_sec <= 4.0:
            return 6
    return max(2, round(duration_sec / 0.35))


def _trim_short_speech_transcript(
    text: str,
    *,
    duration_sec: float,
    speech_sec: float | None = None,
    vc: bool = False,
) -> str:
    """
    Trim Whisper padding/hallucination on short utterances (e.g. '1 2 3 4.' -> '1 2 3').
    """
    t = (text or "").strip()
    if not t:
        return t
    speech_sec = _cap_speech_seconds(duration_sec, speech_sec)
    count_parts = _dedupe_repeated_count_run(_extract_first_counting_run(t))
    if count_parts:
        if _text_matches_whisper_prompt(t, _VC_COUNTING_GPT_PROMPT) or _text_matches_whisper_prompt(
            t, _VC_COUNTING_WHISPER_PROMPT
        ):
            max_tokens = _counting_max_tokens(duration_sec, vc=True)
        else:
            has_speech_timing = bool(speech_sec and speech_sec > 0.2)
            if has_speech_timing:
                eff = float(speech_sec)
                max_tokens = max(2, round(eff / 0.35))
            else:
                eff = min(duration_sec, max(0.45, len(count_parts) * 0.38))
                max_tokens = max(2, round(eff / 0.35))
            if not has_speech_timing and len(count_parts) >= 3:
                max_tokens = min(max_tokens, len(count_parts) - 1)
            if vc:
                max_tokens = min(max_tokens, _counting_max_tokens(duration_sec, vc=True))
        if vc and duration_sec < 3.0 and len(count_parts) >= 5:
            max_tokens = min(max_tokens, _counting_max_tokens(duration_sec, vc=True))
        if len(count_parts) > max_tokens:
            count_parts = count_parts[:max_tokens]
        return " ".join(count_parts)
    eff = speech_sec if speech_sec and speech_sec > 0.2 else duration_sec
    words = t.split()
    max_words = max(2, int(eff * 2.0) + 1)
    if len(words) > max_words:
        return " ".join(words[:max_words])
    return t


def _segments_fail_quality(
    tr: Any,
    *,
    max_no_speech: float,
    max_compression: float,
    text: str,
    min_avg_logprob: float = -0.7,
    vc_lenient: bool = False,
) -> str | None:
    """Reject noise/hallucination; do not drop real speech solely on no_speech_prob."""
    segments = getattr(tr, "segments", None)
    if not segments:
        return None
    t = (text or "").strip()
    if vc_lenient:
        if not t:
            worst_nsp = max(
                float(getattr(seg, "no_speech_prob", 0) or 0) for seg in segments
            )
            if worst_nsp > min(0.98, max_no_speech + 0.45):
                return f"no_speech_prob={worst_nsp:.2f}"
        return None
    logprobs: list[float] = []
    for seg in segments:
        cr = float(getattr(seg, "compression_ratio", 0) or 0)
        if cr > max_compression:
            return f"compression_ratio={cr:.2f}"
        nsp = float(getattr(seg, "no_speech_prob", 0) or 0)
        if not t and nsp > max_no_speech:
            return f"no_speech_prob={nsp:.2f}"
        lp = getattr(seg, "avg_logprob", None)
        if lp is not None:
            logprobs.append(float(lp))
    if logprobs and min(logprobs) < min_avg_logprob and not _is_counting_or_numbers(t):
        return f"avg_logprob={min(logprobs):.2f}"
    if t and len(t) < 20 and not _is_counting_or_numbers(t):
        worst_nsp = max(
            float(getattr(seg, "no_speech_prob", 0) or 0) for seg in segments
        )
        if worst_nsp > min(0.95, max_no_speech + 0.35):
            return f"no_speech_prob={worst_nsp:.2f}"
    return None





def _text_plausible_for_audio(
    text: str, *, duration_sec: float, peak: int, min_peak: int
) -> str | None:
    """Return reason if transcript is unlikely to match the audio chunk."""
    t = (text or "").strip()
    if not t:
        return "empty"
    if peak < min_peak:
        return f"peak {peak} < min_peak_amplitude {min_peak}"
    if _is_counting_or_numbers(t):
        return None
    lower = t.lower()
    for phrase in _WHISPER_HALLUCINATION_PHRASES:
        if phrase in lower:
            return f"blocked phrase ({phrase!r})"
    # Short literal speech (e.g. "yes", "no", "ok")
    if len(t) <= 3 and not t.isdigit():
        return None
    words = t.split()
    if duration_sec > 0 and words:
        wps = len(words) / duration_sec
        avg_len = sum(len(w) for w in words) / len(words)
        if wps > 3.6 and avg_len > 3.8 and len(words) >= 5:
            return f"implausible speech rate ({wps:.1f} words/s)"
        if duration_sec < 2.0 and len(words) > 5 and avg_len > 3.5:
            return f"too much text for {duration_sec:.1f}s audio"
    return None


def _text_matches_whisper_prompt(text: str, prompt: str | None) -> bool:
    """Whisper often repeats the initial prompt on garbled Discord VC audio."""
    if not prompt or not text:
        return False
    t = " ".join(text.lower().split())
    p = " ".join(prompt.lower().split())
    if len(p) < 10:
        return False
    if t == p or t in p or p in t:
        return True
    t_words = set(t.split())
    p_words = set(p.split())
    if len(t_words) >= 2 and len(t_words & p_words) >= max(2, int(len(t_words) * 0.55)):
        return True
    return False


def _text_blocked_hallucination_phrase(text: str, *, whisper_prompt: str | None = None) -> str | None:
    """Block known fake Whisper phrases only (used for Discord VC)."""
    t = (text or "").strip()
    if not t:
        return "empty"
    if _text_matches_whisper_prompt(t, whisper_prompt):
        return "whisper prompt echo"
    lower = t.lower()
    for phrase in _WHISPER_HALLUCINATION_PHRASES:
        if phrase in lower:
            return f"blocked phrase ({phrase!r})"
    return None


def _is_counting_prompt_echo(text: str) -> bool:
    """Whisper/GPT often loops the digit retry prompt on garbled VC audio."""
    parts = _extract_first_counting_run(text)
    if len(parts) < 5:
        return False
    if _text_matches_whisper_prompt(text, _VC_COUNTING_WHISPER_PROMPT):
        return True
    if _text_matches_whisper_prompt(text, _VC_COUNTING_GPT_PROMPT):
        return True
    if len(parts) >= 8:
        return True
    if len(parts) >= 6:
        joined = " ".join(parts)
        if joined.count("1 2 3") >= 2 or joined.count("10") >= 2:
            return True
    return False


def _discord_vc_transcript_ok(
    text: str,
    *,
    duration_sec: float,
    raw_peak: int,
    clipped: bool,
    whisper_prompt: str | None = None,
) -> str | None:
    """Reject common Discord VC Whisper hallucinations (garbled audio → fake phrases)."""
    block = _text_blocked_hallucination_phrase(text, whisper_prompt=whisper_prompt)
    if block:
        return block
    if _is_counting_or_numbers(text):
        if _is_counting_prompt_echo(text):
            return "counting prompt echo"
        return None
    t = (text or "").strip()
    if not t:
        return "empty"
    words = t.split()
    if len(t) > 6 and t.isupper() and len(words) >= 2:
        return "all-caps VC hallucination"
    return None


def _is_gpt_transcribe_model(model: str) -> bool:
    """OpenAI speech models (gpt-4o-*-transcribe) use json/text, not whisper verbose_json."""
    m = (model or "").strip().lower()
    return m.startswith("gpt-") and "transcribe" in m


def _whisper_transcribe(

    oai: Any,

    wav_bytes: bytes,

    *,

    whisper_model: str,

    lang: str | None,

    temperature: float,

    max_no_speech: float,

    max_compression: float,

    min_avg_logprob: float = -0.7,

    prompt: str | None = None,

    vc_lenient: bool = False,

) -> tuple[str | None, str | None, float | None]:

    """

    Run OpenAI speech-to-text (whisper-1 or gpt-4o-*-transcribe). Returns (text, reject_reason).

    text is None when rejected or empty.

    """

    buf = io.BytesIO(wav_bytes)

    buf.name = "chunk.wav"

    use_gpt_transcribe = _is_gpt_transcribe_model(whisper_model)
    kwargs: dict[str, Any] = {
        "model": whisper_model,
        "file": buf,
        "temperature": temperature,
    }
    if lang:
        kwargs["language"] = lang
    if prompt:
        kwargs["prompt"] = prompt[:800]

    if use_gpt_transcribe:
        kwargs["response_format"] = "json"
        kwargs["include"] = ["logprobs"]
    else:
        kwargs["response_format"] = "verbose_json"

    try:
        tr = oai.audio.transcriptions.create(**kwargs)
    except Exception:
        buf.seek(0)
        if use_gpt_transcribe:
            kwargs.pop("include", None)
            kwargs["response_format"] = "text"
        else:
            kwargs.pop("response_format", None)
        try:
            tr = oai.audio.transcriptions.create(**kwargs)
        except Exception:
            return None, "transcription API error", None

    text = (getattr(tr, "text", None) or "").strip()
    speech_sec = _speech_duration_from_transcription(tr)
    if use_gpt_transcribe:
        seg_reason = None
    else:
        seg_reason = _segments_fail_quality(
            tr,
            max_no_speech=max_no_speech,
            max_compression=max_compression,
            text=text,
            min_avg_logprob=min_avg_logprob,
            vc_lenient=vc_lenient,
        )
    if seg_reason:
        return None, f"segment quality ({seg_reason})", speech_sec
    return text, None, speech_sec





def transcribe_process_entry(

    config_path: str,

    in_queue: "multiprocessing.Queue",

    gui_mirror_queue: "multiprocessing.Queue | None" = None,

) -> None:

    """multiprocessing.Process target for Whisper (optional Discord post + GUI mirror)."""

    logging.basicConfig(

        level=logging.INFO,

        format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",

        stream=sys.stdout,

        force=True,

    )

    install_ascii_console_logging()

    root = Path(config_path).resolve().parent

    os.chdir(str(root))



    cfg = load_config_dict(config_path)

    sec = cfg.get("discord_voice_transcribe") or {}

    discord_cfg = cfg.get("discord") or {}

    token = discord_cfg.get("bot_token")

    if not token:

        log.error("[Transcribe] No discord.bot_token")

        return



    prevent_posts = config_bool(sec.get("prevent_discord_transcript_posts"), True)
    post_discord = (
        not prevent_posts
        and config_bool(sec.get("post_transcripts_to_discord"), False)
        and os.environ.get("HOMEY_DISCORD_TRANSCRIPT_POSTS", "").strip().lower()
        not in ("0", "false", "no", "off")
    )
    out_ch_id: int | None = None
    if post_discord:
        try:
            out_ch_id = discord_snowflake_id(sec.get("output_channel_id") or 0)
        except (TypeError, ValueError):
            out_ch_id = 0
        if out_ch_id <= 0:
            log.error(
                "[Transcribe] post_transcripts_to_discord=true but output_channel_id invalid — "
                "Discord posting disabled"
            )
            post_discord = False
            out_ch_id = None
    elif config_bool(sec.get("post_transcripts_to_discord"), False):
        log.info(
            "[Transcribe] post_transcripts_to_discord=true ignored "
            "(prevent_discord_transcript_posts=true)"
        )



    whisper_model = str(sec.get("whisper_model", "whisper-1"))

    opts = _transcribe_settings(sec)



    try:

        oai = make_openai_client(cfg)

    except Exception as e:

        log.error("[Transcribe] OpenAI client failed: %s", e)

        return



    http: httpx.Client | None = None

    if post_discord:
        http = _discord_rest_http_client(cfg)
        log.info("[Transcribe] Worker ready — Whisper -> Discord channel %s + console/GUI", out_ch_id)
    elif prevent_posts:
        log.info(
            "[Transcribe] Worker ready — Whisper -> console/GUI only "
            "(prevent_discord_transcript_posts=true; Discord text posts disabled)"
        )
    else:
        log.info(
            "[Transcribe] Worker ready — Whisper -> console/GUI only (post_transcripts_to_discord=false)"
        )



    try:

        _run_transcribe_loop(

            in_queue,

            oai,

            http,

            str(token) if token else "",

            out_ch_id,

            whisper_model,

            opts,

            gui_mirror_queue,

            post_discord=post_discord,

        )

    finally:

        if http is not None:
            http.close()





def _mirror_gui(
    gui_mirror_queue: "multiprocessing.Queue | None",
    uid: int,
    display_name: str,
    text: str,
) -> None:

    if gui_mirror_queue is None:

        return

    try:
        uid = int(uid)
        if uid == -2:
            kind = "local"
            name = ""
        elif uid > 0:
            kind = "discord_vc"
            name = (display_name or "").strip() or str(uid)
        elif uid == -1:
            kind = "discord_phone"
            name = (display_name or "").strip() or "Call"
        else:
            log.debug("[Transcribe] Unknown capture uid=%s — mirroring as discord_phone", uid)
            kind = "discord_phone"
            name = (display_name or "").strip() or "Call"

        gui_mirror_queue.put((kind, name, text), timeout=1.0)

    except Exception:

        log.debug("[Transcribe] GUI mirror queue full or dead", exc_info=True)


def _mirror_vc_heard(
    gui_mirror_queue: "multiprocessing.Queue | None",
    display_name: str,
    detail: str,
) -> None:
    if gui_mirror_queue is None:
        return
    try:
        gui_mirror_queue.put(("discord_heard", display_name, detail), timeout=0.5)
    except Exception:
        pass





def _run_transcribe_loop(

    in_queue: "multiprocessing.Queue",

    oai: Any,

    http: httpx.Client | None,

    token: str,

    out_ch_id: int | None,

    whisper_model: str,

    opts: dict[str, Any],

    gui_mirror_queue: "multiprocessing.Queue | None",

    *,

    post_discord: bool,

) -> None:

    min_peak = int(opts["min_peak"])
    recent_vc_transcripts: dict[int, tuple[str, float]] = {}
    vc_dedupe_seconds = float(opts.get("vc_dedupe_seconds", 10.0))

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

        is_local_mic = int(_uid) == -2
        is_discord_vc = int(_uid) > 0
        whisper_no_speech = opts["max_no_speech"]
        whisper_compression = opts["max_compression"]
        whisper_logprob = float(opts["min_avg_logprob"])
        whisper_prompt_use = str(opts.get("whisper_prompt") or "")
        if is_local_mic:
            min_peak = int(opts["local_min_peak"])
            max_input_peak = int(opts["local_max_input_peak"])
            scale_target = int(opts["local_scale_target"])
            whisper_no_speech = opts["local_max_no_speech"]
            whisper_compression = opts["local_max_compression"]
            display_name = "LOCAL"
        elif is_discord_vc:
            min_peak = int(opts["vc_min_peak"])
            max_input_peak = int(opts["vc_max_input_peak"])
            scale_target = int(opts["vc_scale_target"])
            whisper_no_speech = opts["vc_max_no_speech"]
            whisper_compression = opts["vc_max_compression"]
            whisper_logprob = float(opts["vc_min_avg_logprob"])
            whisper_prompt_use = str(opts.get("vc_whisper_prompt") or "")
        else:
            min_peak = int(opts["min_peak"])
            max_input_peak = int(opts["max_input_peak"])
            scale_target = 24000

        raw_peak = wav_peak_amplitude(wav_bytes)
        if is_discord_vc:
            log.info(
                "[Transcribe] Discord VC chunk from %s (%d bytes wav, raw_peak=%s)",
                display_name,
                len(wav_bytes),
                raw_peak,
            )
            if opts.get("mirror_vc_activity"):
                dur = _wav_duration_seconds(wav_bytes)
                _mirror_vc_heard(
                    gui_mirror_queue,
                    display_name,
                    f"heard ~{dur:.1f}s (transcribing…)",
                )

        if raw_peak < min_peak:
            log.log(
                logging.INFO if is_discord_vc else logging.DEBUG,
                "[Transcribe] Skipping %s — too quiet (peak=%s < %s)",
                display_name,
                raw_peak,
                min_peak,
            )
            continue

        wav_for_whisper, proc_peak, clipped = (
            pick_vc_wav_for_whisper(
                wav_bytes,
                raw_peak=raw_peak,
                scale_target=scale_target,
                min_peak=min_peak,
                max_input_peak=max_input_peak,
            )
            if is_discord_vc
            else wav_prepare_for_whisper(wav_bytes, scale_target=scale_target)
        )
        if is_discord_vc and (clipped or raw_peak >= 28000):
            # Long counting prompts are echoed as fake transcripts on VC; keep prompt empty.
            whisper_prompt_use = ""
        if clipped or (is_discord_vc and raw_peak >= 28000):
            log.info(
                "[Transcribe] %s clipped (raw peak=%s); scaled to %s for Whisper",
                display_name,
                raw_peak,
                proc_peak,
            )
        if proc_peak < min_peak:
            log.log(
                logging.INFO if is_discord_vc else logging.DEBUG,
                "[Transcribe] Skipping %s — too quiet after processing (peak=%s)",
                display_name,
                proc_peak,
            )
            continue
        if proc_peak >= max_input_peak and is_discord_vc:
            log.warning(
                "[Transcribe] Skipping %s — extreme clip (peak=%s). Lower Discord mic gain.",
                display_name,
                proc_peak,
            )
            continue
        if proc_peak >= max_input_peak and is_local_mic:
            for softer in (22000, 18000, 14000):
                wav_for_whisper, proc_peak, clipped = wav_prepare_for_whisper(
                    wav_bytes, scale_target=softer
                )
                if proc_peak < max_input_peak:
                    break
            if proc_peak >= max_input_peak:
                log.warning(
                    "[Transcribe] Skipping %s — extreme clip (peak=%s). Lower mic gain.",
                    display_name,
                    proc_peak,
                )
                continue
        elif proc_peak >= max_input_peak:
            log.warning(
                "[Transcribe] Skipping %s — still clipped after scaling (peak=%s).",
                display_name,
                proc_peak,
            )
            continue
        log.info(
            "[Transcribe] Whisper processing %s (%d bytes wav, peak=%s)",
            display_name,
            len(wav_for_whisper),
            proc_peak,
        )
        peak = proc_peak



        try:

            text, seg_reject, speech_sec = _whisper_transcribe(

                oai,

                wav_for_whisper,

                whisper_model=whisper_model,

                lang=opts["lang"],

                temperature=opts["temperature"],

                max_no_speech=whisper_no_speech,

                max_compression=whisper_compression,

                min_avg_logprob=whisper_logprob,

                prompt=whisper_prompt_use,

                vc_lenient=is_discord_vc,

            )

            if seg_reject:

                log.warning("[Transcribe] Dropped %s: %s", display_name, seg_reject)

                continue

            if not text and is_discord_vc:
                for softer in (10000, 6000, 4000):
                    if text:
                        break
                    wav_retry, retry_peak, clipped_r = pick_vc_wav_for_whisper(
                        wav_bytes,
                        raw_peak=raw_peak,
                        scale_target=softer,
                        min_peak=min_peak,
                        max_input_peak=max_input_peak,
                    )
                    if retry_peak < min_peak:
                        continue
                    text, seg_reject, speech_sec = _whisper_transcribe(
                        oai,
                        wav_retry,
                        whisper_model=whisper_model,
                        lang=opts["lang"] if not _is_gpt_transcribe_model(whisper_model) else None,
                        temperature=0.0,
                        max_no_speech=whisper_no_speech,
                        max_compression=whisper_compression,
                        min_avg_logprob=whisper_logprob,
                        prompt=None,
                        vc_lenient=True,
                    )
                    if seg_reject or not text:
                        continue
                    wav_for_whisper = wav_retry
                    peak = retry_peak
                    log.info(
                        "[Transcribe] VC empty retry peak=%s for %s: %r",
                        retry_peak,
                        display_name,
                        text[:80],
                    )
                    break

            if not text:

                log.warning(

                    "[Transcribe] Empty transcript for %s (raw_peak=%s, sent_peak=%s) — "
                    "often garbled VC; try local_microphone_transcribe or lower Discord mic gain",

                    display_name,

                    peak,

                    proc_peak,

                )

                continue

            duration_sec = _wav_duration_seconds(wav_for_whisper)
            speech_sec = _cap_speech_seconds(duration_sec, speech_sec)

            should_trim = is_local_mic or _is_counting_or_numbers(text) or bool(
                _extract_first_counting_run(text)
            )
            if should_trim:
                trimmed = _trim_short_speech_transcript(
                    text,
                    duration_sec=duration_sec,
                    speech_sec=speech_sec,
                    vc=is_discord_vc,
                )
                if trimmed != text:
                    log.info(
                        "[Transcribe] Trimmed %s: %r -> %r (speech ~%.1fs)",
                        display_name,
                        text[:80],
                        trimmed,
                        speech_sec or duration_sec,
                    )
                text = trimmed
            if not text.strip():
                continue
            if is_local_mic:
                reject = _text_plausible_for_audio(
                    text, duration_sec=duration_sec, peak=peak, min_peak=min_peak
                )
            elif is_discord_vc:
                reject = _discord_vc_transcript_ok(
                    text,
                    duration_sec=duration_sec,
                    raw_peak=raw_peak,
                    clipped=clipped,
                    whisper_prompt=str(opts.get("vc_whisper_prompt") or ""),
                )
            else:
                reject = _text_blocked_hallucination_phrase(text)

            if reject:

                log.warning(

                    "[Transcribe] Dropped %s (%s): %r",

                    display_name,

                    reject,

                    text[:80],

                )

                continue

            if is_discord_vc:
                norm = " ".join(text.strip().lower().split())
                now = time.monotonic()
                prev = recent_vc_transcripts.get(int(_uid))
                if (
                    prev
                    and prev[0] == norm
                    and (now - prev[1]) < vc_dedupe_seconds
                ):
                    log.info(
                        "[Transcribe] Skipping duplicate VC transcript for %s: %r",
                        display_name,
                        text[:80],
                    )
                    continue
                recent_vc_transcripts[int(_uid)] = (norm, now)

            log.info("[Transcribe] %s: %s", display_name, text)

            _mirror_gui(gui_mirror_queue, _uid, display_name, text)



            if post_discord and http is not None and out_ch_id is not None:
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

            log.exception("[Transcribe] Whisper failed for %s", display_name)


