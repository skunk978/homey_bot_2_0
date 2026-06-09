"""
Smoke tests for the multi-pipeline bot layout (no live Twitch/Discord required).

Run: venviron\\Scripts\\python.exe test_bot_layout.py
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestConfigLayout(unittest.TestCase):
    def test_config_has_new_pipeline_flags(self):
        from discord_voice_common import load_config_dict, receive_vc_audio_enabled

        cfg = load_config_dict("config.yaml")
        sec = cfg.get("discord_voice_transcribe") or {}
        self.assertTrue(sec.get("enabled"))
        self.assertEqual(str(sec.get("discord_audio_source")).lower(), "both")
        self.assertTrue(receive_vc_audio_enabled(sec))
        self.assertTrue(sec.get("tts_to_discord", True))
        self.assertTrue(sec.get("mirror_transcripts_to_gui", True))


class TestGuiFormats(unittest.TestCase):
    def test_live_feed_formats(self):
        # Minimal TwitchBot stub for format helpers
        import yaml

        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        from homey_bot_space_lord import TwitchBot

        class _Stub:
            pass

        bot = _Stub()
        bot._format_live_feed_line = lambda s, u, t: TwitchBot._format_live_feed_line(bot, s, u, t)
        self.assertEqual(bot._format_live_feed_line("LOCAL", "", "hello mic"), "LOCAL - hello mic")
        self.assertEqual(
            bot._format_live_feed_line("DISCORD", "Alice", "hi there"),
            "DISCORD - Alice - hi there",
        )
        self.assertEqual(
            bot._format_live_feed_line("TWITCH", "viewer1", "gg"),
            "TWITCH - viewer1 - gg",
        )


class TestDiscordTranscriptTts(unittest.TestCase):
    def test_should_read_discord_aloud_skips_local_and_self(self):
        import yaml
        from homey_bot_space_lord import TwitchBot

        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        class _Bot:
            config = cfg
            _transcript_is_local_mic = TwitchBot._transcript_is_local_mic
            _read_discord_vc_aloud_enabled = TwitchBot._read_discord_vc_aloud_enabled
            _discord_vc_skip_names = TwitchBot._discord_vc_skip_names
            _is_self_discord_speaker = TwitchBot._is_self_discord_speaker
            _should_read_discord_transcript_aloud = TwitchBot._should_read_discord_transcript_aloud

        bot = _Bot()
        self.assertFalse(bot._should_read_discord_transcript_aloud("local", ""))
        self.assertFalse(bot._should_read_discord_transcript_aloud("discord_vc", "PinnerBob"))
        self.assertTrue(bot._should_read_discord_transcript_aloud("discord_vc", "Eugene"))


class TestTranscribeMirror(unittest.TestCase):
    def test_mirror_queue_kinds(self):
        from discord_transcribe_process import _mirror_gui

        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        _mirror_gui(q, -2, "", "local speech")
        _mirror_gui(q, 42, "Bob", "discord speech")
        local = q.get(timeout=2)
        discord = q.get(timeout=2)
        self.assertEqual(local, ("local", "", "local speech"))
        self.assertEqual(discord, ("discord_vc", "Bob", "discord speech"))


class TestDiscordSpeak(unittest.TestCase):
    def test_generate_wav(self):
        if sys.platform != "win32":
            self.skipTest("Windows SAPI only")
        from discord_voice_speak import generate_speech_wav

        path = generate_speech_wav("test one two", voice_hint="female")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        self.assertGreater(os.path.getsize(path), 44)
        try:
            os.remove(path)
        except OSError:
            pass


class TestTwitchBotHelpers(unittest.TestCase):
    def test_tts_to_discord_enabled(self):
        import yaml
        from homey_bot_space_lord import TwitchBot

        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        class _Minimal:
            config = cfg

            _tts_to_discord_enabled = TwitchBot._tts_to_discord_enabled

        self.assertTrue(_Minimal()._tts_to_discord_enabled())

    def test_conversation_queue_feed(self):
        import yaml
        from homey_bot_space_lord import TwitchBot

        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        q: queue.Queue = queue.Queue()
        recent: list[str] = []

        class _FakeSpaceLord:
            def _append_recent_chat(self, user, msg):
                recent.append(f"{user}: {msg}")

        class _Bot:
            config = cfg
            space_lord = _FakeSpaceLord()
            _conversation_queue = q
            _enqueue_conversation = TwitchBot._enqueue_conversation

        _Bot()._enqueue_conversation("LOCAL", "You", "testing local")
        _Bot()._enqueue_conversation("DISCORD", "Alice", "testing discord")
        _Bot()._enqueue_conversation("TWITCH", "bob", "testing twitch")
        self.assertEqual(q.qsize(), 3)
        items = [q.get_nowait() for _ in range(3)]
        self.assertEqual(items[0], ("LOCAL", "You", "testing local"))
        self.assertEqual(items[1], ("DISCORD", "Alice", "testing discord"))
        self.assertEqual(items[2], ("TWITCH", "bob", "testing twitch"))
        self.assertEqual(len(recent), 3)

    def test_conversation_worker_target_is_callable(self):
        """Instance attr must not shadow _conversation_worker_loop method (Thread target=None bug)."""
        import inspect
        import yaml
        from homey_bot_space_lord import TwitchBot

        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        class _MinimalInit:
            """Mimic TwitchBot.__init__ attribute that shadowed the worker method."""

            def __init__(self):
                self._conversation_worker_event_loop = None

            _conversation_worker_loop = TwitchBot._conversation_worker_loop

        bot = _MinimalInit()
        target = bot._conversation_worker_loop
        self.assertTrue(callable(target), "worker loop must be callable, not None")
        self.assertEqual(getattr(target, "__name__", None), "_conversation_worker_loop")


class TestSpaceLordPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_skips_when_should_not_respond(self):
        import yaml
        from homey_bot_space_lord import TwitchBot

        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        spoken: list[str] = []

        class _FakeSL:
            async def should_respond(self, user, msg, append_context=False):
                return False

            async def respond_to_chat(self, user, msg, append_context=False):
                return "should not run"

        class _Bot:
            config = cfg
            space_lord = _FakeSL()

            async def _speak_to_discord(self, text, voice="female"):
                spoken.append(text)
                return True

            _format_live_feed_line = TwitchBot._format_live_feed_line
            _run_space_lord_pipeline = TwitchBot._run_space_lord_pipeline

        await _Bot()._run_space_lord_pipeline("TWITCH", "x", "hello")
        self.assertEqual(spoken, [])

    async def test_pipeline_speaks_when_should_respond(self):
        import yaml
        from homey_bot_space_lord import TwitchBot

        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        spoken: list[str] = []

        class _FakeSL:
            async def should_respond(self, user, msg, append_context=False):
                return True

            async def respond_to_chat(self, user, msg, append_context=False):
                return "Greetings, mortal."

        class _Bot:
            config = cfg
            space_lord = _FakeSL()

            async def _speak_to_discord(self, text, voice="female"):
                spoken.append(text)
                return True

            _format_live_feed_line = TwitchBot._format_live_feed_line
            _run_space_lord_pipeline = TwitchBot._run_space_lord_pipeline

        await _Bot()._run_space_lord_pipeline("TWITCH", "viewer", "hey space lord")
        self.assertEqual(len(spoken), 1)
        self.assertIn("Greetings, mortal", spoken[0])


class TestListenProcessEntry(unittest.TestCase):
    def test_listen_entry_accepts_speak_queue(self):
        import inspect
        from discord_voice_listen_process import listen_process_entry

        sig = inspect.signature(listen_process_entry)
        self.assertIn("speak_queue", sig.parameters)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (
        TestConfigLayout,
        TestGuiFormats,
        TestTranscribeMirror,
        TestDiscordSpeak,
        TestTwitchBotHelpers,
        TestSpaceLordPipeline,
        TestListenProcessEntry,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
