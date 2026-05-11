#!/usr/bin/env python3
"""
Homey Bot (host audio) — Twitch chat reader with Windows TTS for OBS-capable playback

This bot reads Twitch chat messages and speaks them aloud using Windows TTS with female voice.
No Discord dependency - direct audio output that can be captured by OBS.

AUDIO OUTPUT OPTIONS:
- "default": Outputs to desktop speakers (best for OBS capture)
- "bluetooth": Outputs to Bluetooth devices
- Specific device name: Outputs to specific audio device

For OBS streaming: Use "default" audio device to output to desktop speakers
For Bluetooth listening: Use "bluetooth" or specific Bluetooth device name

TTS: Windows TTS with automatic female voice detection
"""

import asyncio
import logging
import multiprocessing
import os
from typing import Any
import time
import yaml
import subprocess
import twitchio
from twitchio import eventsub
from twitchio.exceptions import HTTPException as TwitchHTTPException
from twitchio.exceptions import InvalidTokenException
from twitchio.ext import commands as twitch_commands
from asyncio import QueueEmpty
import threading
import pygame
import pyaudio
import wave
import tempfile
import openai
import json
import discord
from discord.ext import commands as discord_commands
import speech_recognition as sr
import pyaudio
import wave
import threading
import queue

# Configure logging first
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)


def _log_space_lord_openai_error(context: str, exc: BaseException) -> None:
    """Log OpenAI/SDK failures with a short hint when the symptom is connectivity."""
    ename = type(exc).__name__
    emsg = (str(exc) or "").strip() or repr(exc)
    hay = (ename + " " + emsg).lower()
    hint = ""
    if any(x in hay for x in ("connection", "connecterror", "timeout", "timed out", "network", "unreachable")):
        hint = (
            " — Check internet, VPN, firewall/proxy (must allow HTTPS to api.openai.com). "
            "Optional in config.yaml: openai.timeout_seconds (e.g. 90), openai.max_retries (e.g. 4), "
            "openai.base_url if you use a compatible gateway."
        )
    logger.error("[SpaceLord] %s [%s]: %s%s", context, ename, emsg, hint, exc_info=True)


def _make_openai_client(config: dict) -> openai.OpenAI:
    """Build OpenAI client; supports optional timeout, retries, base_url from config.

    Uses the OS TLS trust store via ``truststore`` when available so corporate/AV HTTPS
    inspection works where the default CA bundle fails. Set ``openai.use_system_ca`` to false
    to use the OpenSSL default bundle, or ``openai.tls_verify`` to false for debug only.
    """
    import ssl

    oc = config.get("openai") or {}
    api_key = oc.get("api_key")
    if not api_key:
        raise ValueError("config openai.api_key is required")
    kwargs: dict = {"api_key": str(api_key).strip()}
    if oc.get("timeout_seconds") is not None:
        kwargs["timeout"] = float(oc["timeout_seconds"])
    if oc.get("max_retries") is not None:
        kwargs["max_retries"] = int(oc["max_retries"])
    bu = oc.get("base_url")
    if isinstance(bu, str) and bu.strip():
        kwargs["base_url"] = bu.strip()

    if oc.get("tls_verify") is False:
        import httpx

        kwargs["http_client"] = httpx.Client(verify=False)
        logger.warning(
            "[OpenAI] tls_verify=false — HTTPS certificate verification disabled (use only for debugging)."
        )
    elif oc.get("use_system_ca", True):
        try:
            import httpx
            import truststore

            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            kwargs["http_client"] = httpx.Client(verify=ctx)
        except ImportError:
            logger.warning(
                "[OpenAI] truststore not installed — using default TLS roots "
                "(install truststore if you hit CERTIFICATE_VERIFY_FAILED to api.openai.com)."
            )

    return openai.OpenAI(**kwargs)


def _twitch_oauth_validate_sync(access_token: str) -> dict | None:
    """GET https://id.twitch.tv/oauth2/validate (sync). Caller must never log secrets."""
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {access_token.strip()}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = ""
        logger.warning("[Twitch][debug] validate HTTPError %s: %s", e.code, body[:500])
        return None
    except Exception as exc:
        logger.warning("[Twitch][debug] validate request failed: %s", exc)
        return None


# Import Windows TTS
try:
    import win32com.client
    WINDOWS_TTS_AVAILABLE = True
    logger.info("[TTS] ✅ Windows TTS available")
except ImportError:
    WINDOWS_TTS_AVAILABLE = False
    logger.error("[TTS] ❌ Windows TTS not available - install pywin32")

# Try to import GUI module
try:
    from gui_monitor import add_gui_message, start_gui
    GUI_AVAILABLE = True
    logger.info("[GUI] ✅ GUI module loaded successfully")
except ImportError:
    GUI_AVAILABLE = False
    logger.info("[GUI] ⚠️ GUI module not available - running in console mode only")

        

class DesktopAudioPlayer:
    """Handles desktop audio output for TTS messages - can output to any audio device including Bluetooth."""
    
    def __init__(self, audio_device="default"):
        self.audio_queue = asyncio.Queue(maxsize=20)
        self.is_playing = False
        self.audio_task = None
        self.volume = 0.8
        self.sample_rate = 22050
        self.channels = 1
        self.chunk_size = 1024
        self.audio_device = audio_device  # "default", "bluetooth", or specific device name
        self.pyaudio_instance = None
        self.audio_stream = None
        self.device_index = None
        
        # For OBS capture: use "default" to output to desktop speakers
        # For Bluetooth: use "bluetooth" or specific Bluetooth device name
        # For specific device: use exact device name from Windows audio settings
        
        # Initialize PyAudio for better device control
        try:
            self.pyaudio_instance = pyaudio.PyAudio()
            self.device_index = self._find_audio_device()
            playback_label = self.audio_device
            if self.device_index is not None:
                try:
                    playback_label = self.pyaudio_instance.get_device_info_by_index(self.device_index).get(
                        "name", str(self.audio_device)
                    )
                except Exception:
                    pass
            if isinstance(playback_label, str) and isinstance(self.audio_device, str):
                requested = self.audio_device.strip().lower()
                if requested not in ("default", "pc", "auto", "bluetooth", "") and playback_label.lower() != self.audio_device.strip().lower():
                    logger.warning(
                        "[DesktopAudio] Requested audio.device %r was not matched; playback is routed to %r (index %s). "
                        "Copy the exact name from the device list above into config.yaml if you need this output.",
                        self.audio_device,
                        playback_label,
                        self.device_index,
                    )
            logger.info(
                "[DesktopAudio] ✅ PyAudio initialized; playback device: %s (index %s); config label: %s",
                playback_label,
                self.device_index,
                self.audio_device,
            )
            
        except Exception as e:
            logger.error(f"[DesktopAudio] ❌ Failed to initialize PyAudio: {e}")
            # Fallback to pygame if PyAudio fails
            try:
                pygame.mixer.init(frequency=self.sample_rate, size=-16, channels=self.channels)
                pygame.mixer.music.set_volume(self.volume)
                logger.info(f"[DesktopAudio] ✅ Fallback to pygame mixer initialized")
            except Exception as pygame_error:
                logger.error(f"[DesktopAudio] ❌ Both PyAudio and pygame failed: {pygame_error}")
                raise
    
    def _find_audio_device(self):
        """Find the appropriate audio device index based on the device name."""
        try:
            if not self.pyaudio_instance:
                logger.warning("[DesktopAudio] ⚠️ PyAudio not available, using default device")
                return None
            
            # Get list of all audio devices
            device_count = self.pyaudio_instance.get_device_count()
            logger.info(f"[DesktopAudio] 🔍 Found {device_count} audio devices")
            
            # List all available devices for debugging
            logger.info(f"[DesktopAudio] 🔍 Available audio devices:")
            for i in range(device_count):
                device_info = self.pyaudio_instance.get_device_info_by_index(i)
                device_name = device_info.get('name', 'Unknown')
                max_output_channels = device_info.get('maxOutputChannels', 0)
                if max_output_channels > 0:  # Only show output devices
                    logger.info(f"[DesktopAudio]   {i}: {device_name} (output channels: {max_output_channels})")
            
            # Handle different device selection modes ('pc' and legacy chr-built alias match default routing)
            _mode = (self.audio_device or "").strip().lower()
            _legacy_alias = "".join(map(chr, (100, 101, 115, 107, 116, 111, 112)))
            if _mode in ("default", "pc", "auto") or _mode == "" or _mode == _legacy_alias:
                # Use default output device
                default_device = self.pyaudio_instance.get_default_output_device_info()
                device_index = default_device['index']
                logger.info(f"[DesktopAudio] 🎯 Using default device: {default_device['name']}")
                return device_index
            
            elif _mode == "bluetooth":
                # Look for Bluetooth devices
                bluetooth_devices = []
                for i in range(device_count):
                    device_info = self.pyaudio_instance.get_device_info_by_index(i)
                    device_name = device_info.get('name', '').lower()
                    max_output_channels = device_info.get('maxOutputChannels', 0)
                    
                    # Check if it's a Bluetooth device and has output capability
                    if ('bluetooth' in device_name or 'bt' in device_name) and max_output_channels > 0:
                        bluetooth_devices.append((i, device_info))
                        logger.info(f"[DesktopAudio] 🎧 Found Bluetooth device: {device_info['name']}")
                
                if bluetooth_devices:
                    # Use the first available Bluetooth device
                    device_index, device_info = bluetooth_devices[0]
                    logger.info(f"[DesktopAudio] 🎯 Using Bluetooth device: {device_info['name']}")
                    return device_index
                else:
                    logger.warning("[DesktopAudio] ⚠️ No Bluetooth devices found, using default")
                    return self.pyaudio_instance.get_default_output_device_info()['index']
            
            else:
                # Look for specific device by name with more flexible matching
                target_device_lower = self.audio_device.lower()
                logger.info(f"[DesktopAudio] 🔍 Looking for device: '{self.audio_device}'")
                
                # First pass: look for exact or close matches
                candidates = []
                for i in range(device_count):
                    device_info = self.pyaudio_instance.get_device_info_by_index(i)
                    device_name = device_info.get('name', '')
                    max_output_channels = device_info.get('maxOutputChannels', 0)
                    
                    if max_output_channels > 0:  # Only consider output devices
                        device_name_lower = device_name.lower()
                        
                        # More flexible matching - check for key parts of the device name
                        if (target_device_lower in device_name_lower or 
                            device_name_lower in target_device_lower or
                            any(part in device_name_lower for part in target_device_lower.split() if len(part) > 3)):
                            candidates.append((i, device_name, device_name_lower))
                
                # Prioritize "Headphones" over "Headset" for Bluetooth devices
                if candidates:
                    # Sort candidates to prioritize "headphones" over "headset"
                    def sort_key(candidate):
                        _, device_name, device_name_lower = candidate
                        if 'headphones' in device_name_lower and 'headset' not in device_name_lower:
                            return 0  # Highest priority - Headphones without Headset
                        elif 'headset' in device_name_lower:
                            return 1  # Lower priority - Headset devices
                        elif 'headphones' in device_name_lower:
                            return 2  # Medium priority - Headphones with other text
                        else:
                            return 3  # Lowest priority
                    
                    candidates.sort(key=sort_key)
                    best_match = candidates[0]
                    logger.info(f"[DesktopAudio] 🎯 Found matching device: {best_match[1]}")
                    return best_match[0]
                
                # If no specific device found, use default
                logger.warning(f"[DesktopAudio] ⚠️ Device '{self.audio_device}' not found, using default")
                default_device = self.pyaudio_instance.get_default_output_device_info()
                logger.info(f"[DesktopAudio] 🎯 Using default device: {default_device['name']}")
                return default_device['index']
                
        except Exception as e:
            logger.error(f"[DesktopAudio] ❌ Error finding audio device: {e}")
            # Return default device index as fallback
            try:
                return self.pyaudio_instance.get_default_output_device_info()['index']
            except:
                return None
    
    async def start_audio_processor(self):
        """Start the audio queue processor."""
        if not self.audio_task:
            self.audio_task = asyncio.create_task(self._process_audio_queue())
            # Start simple periodic cleanup task (every 5 minutes)
            asyncio.create_task(self._periodic_cleanup())
            logger.info("[DesktopAudio] 🚀 Audio queue processor started")
            logger.info(f"[DesktopAudio] 🎵 Audio device: {self.audio_device}")
    
    async def stop_audio_processor(self):
        """Stop the audio queue processor."""
        if self.audio_task:
            self.audio_task.cancel()
            try:
                await self.audio_task
            except asyncio.CancelledError:
                pass
            self.audio_task = None
            logger.info("[DesktopAudio] 🛑 Audio queue processor stopped")
        
        # Clean up PyAudio resources
        if self.pyaudio_instance:
            try:
                self.pyaudio_instance.terminate()
                logger.info("[DesktopAudio] 🛑 PyAudio terminated")
            except Exception as e:
                logger.error(f"[DesktopAudio] ❌ Error terminating PyAudio: {e}")
            self.pyaudio_instance = None
    
    async def play_audio(self, audio_file: str) -> bool:
        """Add audio file to the playback queue."""
        try:
            if not os.path.exists(audio_file):
                logger.error(f"[DesktopAudio] Audio file not found: {audio_file}")
                return False
            
            # Check if queue is full and remove oldest item if needed
            if self.audio_queue.full():
                try:
                    old_file = self.audio_queue.get_nowait()
                    self.audio_queue.task_done()
                    # Clean up old audio file
                    await self._safe_remove_file(old_file)
                    logger.debug("[DesktopAudio] 🗑️ Removed oldest audio from queue")
                except asyncio.QueueEmpty:
                    pass
            
            # Add to queue
            await self.audio_queue.put(audio_file)
            logger.debug(f"[DesktopAudio] 📝 Queued audio: {audio_file}")
            
            # Send to GUI if available
            if GUI_AVAILABLE:
                add_gui_message(f"Queued audio: {os.path.basename(audio_file)}", "INFO")
            
            # Start processor if not already running
            if not self.audio_task:
                await self.start_audio_processor()
            
            return True
            
        except Exception as e:
            logger.error(f"[DesktopAudio] Error queuing audio: {e}")
            return False
    
    async def _process_audio_queue(self):
        """Process audio files from the queue and play them."""
        while True:
            try:
                # Get next audio file
                audio_file = await self.audio_queue.get()
                
                if audio_file is None:  # Shutdown signal
                    break
                
                # Play the audio
                await self._play_single_audio(audio_file)
                
                # Mark as done
                self.audio_queue.task_done()
                
                # Wait a moment to ensure file is no longer in use
                await asyncio.sleep(0.2)
                
                # Clean up the temporary audio file with retry logic
                await self._safe_remove_file(audio_file)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DesktopAudio] Error processing audio: {e}")
                await asyncio.sleep(0.1)
    
    async def _play_single_audio(self, audio_file: str):
        """Play a single audio file through the selected audio device."""
        try:
            logger.info(f"[DesktopAudio] 🔊 Playing: {audio_file}")
            
            # Send to GUI if available
            if GUI_AVAILABLE:
                add_gui_message(f"🔊 Playing audio: {os.path.basename(audio_file)}", "AUDIO")
            
            # Try PyAudio first for better device control
            if self.pyaudio_instance and self.device_index is not None:
                success = await self._play_with_pyaudio(audio_file)
                if success:
                    logger.debug(f"[DesktopAudio] ✅ Finished playing with PyAudio: {audio_file}")
                    if GUI_AVAILABLE:
                        add_gui_message(f"✅ Finished playing: {os.path.basename(audio_file)}", "AUDIO")
                    return
                else:
                    logger.warning("[DesktopAudio] ⚠️ PyAudio playback failed, falling back to pygame")
            
            # Fallback to pygame if PyAudio fails or is not available
            await self._play_with_pygame(audio_file)
            
        except Exception as e:
            logger.error(f"[DesktopAudio] Error playing audio: {e}")
            if GUI_AVAILABLE:
                add_gui_message(f"❌ Audio playback error: {str(e)[:50]}", "ERROR")
    
    async def _play_with_pyaudio(self, audio_file: str) -> bool:
        """Play audio using PyAudio with specific device selection."""
        try:
            # Open the audio file
            with wave.open(audio_file, 'rb') as wf:
                # Get audio file parameters
                file_sample_rate = wf.getframerate()
                file_channels = wf.getnchannels()
                file_sample_width = wf.getsampwidth()
                
                # Log which device we're using
                if self.device_index is not None:
                    device_info = self.pyaudio_instance.get_device_info_by_index(self.device_index)
                    device_name = device_info.get('name', 'Unknown')
                    logger.info(f"[DesktopAudio] 🎯 Playing audio on device: {device_name}")
                else:
                    logger.info(f"[DesktopAudio] 🎯 Playing audio on default device")
                
                # Open PyAudio stream with the selected device
                stream = self.pyaudio_instance.open(
                    format=self.pyaudio_instance.get_format_from_width(file_sample_width),
                    channels=file_channels,
                    rate=file_sample_rate,
                    output=True,
                    output_device_index=self.device_index
                )
                
                # Read and play audio data
                chunk_size = 1024
                data = wf.readframes(chunk_size)
                
                while data:
                    stream.write(data)
                    data = wf.readframes(chunk_size)
                    await asyncio.sleep(0.01)  # Small delay to prevent blocking
                
                # Clean up
                stream.stop_stream()
                stream.close()
                
                logger.info(f"[DesktopAudio] ✅ PyAudio playback completed: {audio_file}")
                return True
                
        except Exception as e:
            logger.error(f"[DesktopAudio] ❌ PyAudio playback error: {e}")
            return False
    
    async def _play_with_pygame(self, audio_file: str):
        """Fallback audio playback using pygame."""
        try:
            # Load and play the audio file
            pygame.mixer.music.load(audio_file)
            pygame.mixer.music.set_volume(self.volume)
            pygame.mixer.music.play()
            
            # Wait for playback to complete
            while pygame.mixer.music.get_busy():
                await asyncio.sleep(0.1)
            
            # Ensure playback is completely stopped
            pygame.mixer.music.stop()
            
            logger.debug(f"[DesktopAudio] ✅ Finished playing with pygame: {audio_file}")
            
            # Send completion to GUI if available
            if GUI_AVAILABLE:
                add_gui_message(f"✅ Finished playing: {os.path.basename(audio_file)}", "AUDIO")
            
        except Exception as e:
            logger.error(f"[DesktopAudio] ❌ Pygame playback error: {e}")
            raise
    
    def set_volume(self, volume: float):
        """Set the audio volume (0.0 to 1.0)."""
        self.volume = max(0.0, min(1.0, volume))
        logger.info(f"[DesktopAudio] 🎚️ Volume set to: {self.volume:.2f}")
    
    def get_audio_device_info(self):
        """Get information about the current audio device."""
        return {
            "device": self.audio_device,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "volume": self.volume
        }
    
    async def clear_queue(self):
        """Clear all pending audio in the queue."""
        try:
            while not self.audio_queue.empty():
                audio_file = self.audio_queue.get_nowait()
                self.audio_queue.task_done()
                # Clean up audio file
                await self._safe_remove_file(audio_file)
            logger.info("[DesktopAudio] 🗑️ Audio queue cleared")
        except Exception as e:
            logger.error(f"[DesktopAudio] Error clearing queue: {e}")
    
    async def cleanup_temp_files(self):
        """Simple cleanup of temporary files."""
        try:
            import glob
            temp_patterns = ["temp_tts_windows_*.wav", "temp_tts_edge_*.mp3"]
            
            cleaned_count = 0
            for pattern in temp_patterns:
                for temp_file in glob.glob(pattern):
                    try:
                        if os.path.exists(temp_file):
                            os.remove(temp_file)
                            cleaned_count += 1
                            logger.debug(f"[DesktopAudio] 🗑️ Cleaned up: {os.path.basename(temp_file)}")
                    except Exception as e:
                        logger.debug(f"[DesktopAudio] Could not clean up {os.path.basename(temp_file)}: {e}")
            
            if cleaned_count > 0:
                logger.info(f"[DesktopAudio] 🗑️ Cleaned up {cleaned_count} temporary files")
                
        except Exception as e:
            logger.error(f"[DesktopAudio] Error during temp file cleanup: {e}")
    
    async def _periodic_cleanup(self):
        """Simple periodic cleanup of temporary files."""
        while True:
            try:
                # Wait 5 minutes between cleanups
                await asyncio.sleep(300)
                
                # Clean up any temp files that might have been missed
                await self.cleanup_temp_files()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DesktopAudio] Error in periodic cleanup: {e}")
                await asyncio.sleep(60)
    
    async def _safe_remove_file(self, file_path: str):
        """Simple and reliable file removal."""
        if not os.path.exists(file_path):
            return
        
        # Simple approach: try a few times with short delays
        for attempt in range(3):
            try:
                os.remove(file_path)
                logger.debug(f"[DesktopAudio] ✅ Cleaned up: {os.path.basename(file_path)}")
                return
            except PermissionError:
                if attempt < 2:  # Only retry twice
                    await asyncio.sleep(0.1)  # Very short delay
                else:
                    # On final attempt, just log and move on
                    logger.debug(f"[DesktopAudio] ⚠️ Could not delete: {os.path.basename(file_path)} (will be cleaned up later)")
                    # Don't mark for later cleanup - just let it be
                    return
            except Exception as e:
                logger.debug(f"[DesktopAudio] Error deleting {os.path.basename(file_path)}: {e}")
                return
    


class DesktopTTS:
    """Text-to-Speech system using Windows TTS with female voice."""
    
    def __init__(self, audio_player: DesktopAudioPlayer):
        self.audio_player = audio_player
        self.speech_rate = "+30%"
        self.max_message_length = 500  # Increased to allow full Twitch chat messages
        self.audio_cache = {}
        self.cache_size_limit = 100
        self.female_voice = None  # Will be set to best available female voice
        
    async def speak(self, text: str) -> bool:
        """Convert text to speech and queue it for Bluetooth audio playback."""
        try:
            # Truncate very long messages
            if len(text) > self.max_message_length:
                text = text[:self.max_message_length] + "..."
            
            # Check cache first
            cache_key = text.lower().strip()
            if cache_key in self.audio_cache:
                audio_file = self.audio_cache[cache_key]
                if os.path.exists(audio_file):
                    logger.debug(f"[DesktopTTS] 🎯 Cache hit for: {text}")
                    return await self.audio_player.play_audio(audio_file)
            
            # Generate new audio with retry logic
            max_retries = 3
            retry_delay = 2  # seconds
            
            for attempt in range(max_retries):
                try:
                    audio_file = await self._generate_speech(text)
                    if audio_file:
                        # Cache the audio file
                        self.audio_cache[cache_key] = audio_file
                        await self._cleanup_cache()
                        
                        # Send to GUI if available
                        if GUI_AVAILABLE:
                            add_gui_message(f"[DesktopTTS] Generated speech: {text[:30]}...", "TTS")
                        
                        # Queue for playback
                        return await self.audio_player.play_audio(audio_file)
                    
                    # If we get here, TTS failed
                    if attempt < max_retries - 1:
                        logger.warning(f"[DesktopTTS] TTS attempt {attempt + 1} failed, retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        logger.error(f"[DesktopTTS] All {max_retries} TTS attempts failed for: {text}")
                        
                except Exception as e:
                    logger.error(f"[DesktopTTS] Error in attempt {attempt + 1}: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2
            
            return False
            
        except Exception as e:
            logger.error(f"[DesktopTTS] Critical error in speak: {e}")
            return False
    
    async def _generate_speech(self, text: str) -> str:
        """Generate speech audio using Windows TTS with female voice."""
        try:
            if WINDOWS_TTS_AVAILABLE:
                try:
                    logger.info("[DesktopTTS] 🎤 Using Windows TTS with female voice...")
                    return await self._generate_windows_tts(text)
                except Exception as windows_error:
                    logger.error(f"[DesktopTTS] Windows TTS failed: {windows_error}")
            else:
                logger.error("[DesktopTTS] ❌ Windows TTS not available")
            
            return None
                
        except Exception as e:
            logger.error(f"[DesktopTTS] Critical error in speech generation: {e}")
            return None
    

    
    async def _generate_windows_tts(self, text: str) -> str:
        """Generate speech using Windows TTS with female voice."""
        try:
            # Create temporary file
            temp_file = f"temp_tts_windows_{int(time.time())}_{hash(text) % 10000}.wav"
            
            # Run Windows TTS in a thread executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._generate_windows_tts_sync, text, temp_file)
            
            # Give Windows a moment to finish writing the file
            await asyncio.sleep(0.5)
            
            if os.path.exists(temp_file):
                logger.info(f"[DesktopTTS] ✅ Generated Windows TTS for: {text}")
                return temp_file
            else:
                logger.error("[DesktopTTS] ❌ Windows TTS file not created")
                return None
                
        except Exception as e:
            logger.error(f"[DesktopTTS] Windows TTS error: {e}")
            import traceback
            logger.error(f"[DesktopTTS] Traceback: {traceback.format_exc()}")
            return None
    
    def _generate_windows_tts_sync(self, text: str, temp_file: str):
        """Synchronous Windows TTS generation (runs in thread executor)."""
        import pythoncom
        
        try:
            # Initialize COM for this thread
            pythoncom.CoInitialize()
            
            try:
                # Use Windows SAPI TTS
                speaker = win32com.client.Dispatch("SAPI.SpVoice")
                
                # Find and set the best female voice
                if not self.female_voice:
                    self.female_voice = self._find_best_female_voice_sync(speaker)
                
                if self.female_voice:
                    speaker.Voice = self.female_voice
                    voice_desc = self.female_voice.GetDescription()
                    logger.info(f"[DesktopTTS] 🎤 Using female voice: {voice_desc}")
                    
                    # Send to GUI if available
                    if GUI_AVAILABLE:
                        add_gui_message(f"🎤 Using female voice: {voice_desc}", "TTS")
                
                # Generate the speech file
                stream = win32com.client.Dispatch("SAPI.SpFileStream")
                stream.Open(temp_file, 3)  # 3 = SSFMOpenWriteOnly | SSFMOpenCreate
                speaker.AudioOutputStream = stream
                speaker.Speak(text)
                stream.Close()
                
            finally:
                # Uninitialize COM for this thread
                pythoncom.CoUninitialize()
            
        except Exception as e:
            logger.error(f"[DesktopTTS] Windows TTS sync error: {e}")
            raise
    
    async def _find_best_female_voice(self, speaker):
        """Find the best available female voice (async wrapper)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._find_best_female_voice_sync, speaker)
    
    def _find_best_female_voice_sync(self, speaker):
        """Find the best available female voice (synchronous)."""
        # Note: COM is already initialized by the caller (_generate_windows_tts_sync)
        try:
            voices = speaker.GetVoices()
            female_voices = []
            
            # Look for female voices
            for i in range(voices.Count):
                voice = voices.Item(i)
                voice_desc = voice.GetDescription().lower()
                
                # Check for female indicators
                if any(word in voice_desc for word in ["female", "girl", "woman", "zira", "eva", "jenny", "aria"]):
                    female_voices.append(voice)
            
            if female_voices:
                # Prefer Microsoft Zira if available (usually the best)
                for voice in female_voices:
                    if "zira" in voice.GetDescription().lower():
                        logger.info(f"[DesktopTTS] 🎀 Found preferred female voice: {voice.GetDescription()}")
                        return voice
                
                # Otherwise use the first female voice found
                best_voice = female_voices[0]
                logger.info(f"[DesktopTTS] 🎀 Using female voice: {best_voice.GetDescription()}")
                return best_voice
            else:
                logger.warning("[DesktopTTS] ⚠️ No female voices found, using default")
                return None
                
        except Exception as e:
            logger.error(f"[DesktopTTS] Error finding female voice: {e}")
            return None
    

    
    async def _cleanup_cache(self):
        """Clean up old cache entries to prevent disk space issues."""
        if len(self.audio_cache) > self.cache_size_limit:
            # Remove oldest entries
            items_to_remove = len(self.audio_cache) - self.cache_size_limit
            for _ in range(items_to_remove):
                if self.audio_cache:
                    oldest_key = next(iter(self.audio_cache))
                    old_file = self.audio_cache.pop(oldest_key)
                    try:
                        if os.path.exists(old_file):
                            await self.audio_player._safe_remove_file(old_file)
                    except Exception as e:
                        logger.debug(f"[DesktopTTS] Cache cleanup error: {e}")
    
    def set_speech_rate(self, rate: str):
        """Set the speech rate for Windows TTS."""
        self.speech_rate = rate
        logger.info(f"[DesktopTTS] 🎯 Speech rate set to: {rate}")
    
    def get_current_voice(self):
        """Get information about the current voice being used."""
        if self.female_voice:
            return {
                "name": self.female_voice.GetDescription(),
                "id": self.female_voice.GetId(),
                "language": self.female_voice.GetLanguage()
            }
        return None

class SpaceLord:
    """Space Lord AI personality system that responds to Twitch chat."""
    
    def __init__(self, config, tts_system):
        self.config = config
        self.tts_system = tts_system
        self.openai_client = _make_openai_client(config)
        self.persona = self._load_persona()  # Load default first
        self.memories = []
        self.max_memories = 50
        self.last_response_time = 0
        self.response_cooldown = 30  # seconds between responses
        
        # Initialize persona and memories from Discord (will be called asynchronously)
        self.discord_persona_loaded = False
        self.discord_memories = None
        self.discord_memories_loaded = False
        
        # Initialize voice listener for wake word detection
        self.voice_listener = None
        self.is_listening = False
        self.wake_words = ["hey mudflap", "hey space lord", "hey space lord", "mudflap", "space lord"]
    
    async def initialize_discord_persona(self):
        """Initialize Space Lord's persona and memories from Discord channels."""
        try:
            logger.info("[SpaceLord] 🔄 Initializing persona and memories from Discord...")
            
            # Fetch persona
            discord_persona = await self._fetch_persona_from_discord()
            if discord_persona:
                self.persona = discord_persona
                self.discord_persona_loaded = True
                logger.info("[SpaceLord] ✅ Persona loaded from Discord successfully")
                # Save to local file for backup
                self._save_persona(discord_persona)
            else:
                logger.warning("[SpaceLord] ⚠️ Could not load persona from Discord, using local persona")
            
            # Fetch memories
            discord_memories = await self._fetch_memories_from_discord()
            if discord_memories:
                self.discord_memories = discord_memories
                self.discord_memories_loaded = True
                logger.info("[SpaceLord] ✅ Memories loaded from Discord successfully")
            else:
                logger.warning("[SpaceLord] ⚠️ Could not load memories from Discord, using local memories")
            
        except Exception as e:
            logger.error(f"[SpaceLord] ❌ Error initializing Discord persona/memories: {e}")
            logger.info("[SpaceLord] ℹ️ Using local persona and memories as fallback")
        
    def _load_persona(self):
        """Load Space Lord's persona from file or use default."""
        try:
            if os.path.exists("space_lord_persona.txt"):
                with open("space_lord_persona.txt", "r", encoding="utf-8") as f:
                    return f.read().strip()
            else:
                # Default Space Lord persona
                return """You are Space Lord, a charismatic and slightly eccentric space explorer and streamer. You have a deep, commanding voice and speak with authority about space, technology, and life. You're friendly but maintain an air of mystery about your cosmic adventures. You respond to Twitch chat messages with wisdom, humor, and occasional space facts. Keep responses under 100 words and maintain your unique personality."""
        except Exception as e:
            logger.error(f"[SpaceLord] Error loading persona: {e}")
            return "You are Space Lord, a charismatic space explorer."
    
    async def _fetch_persona_from_discord(self):
        """Fetch Space Lord's persona from Discord channel."""
        try:
            if not self.config.get('discord', {}).get('bot_token'):
                logger.warning("[SpaceLord] ⚠️ No Discord bot token configured, using local persona")
                return None
            
            # Create Discord client
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)
            
            persona_content = []
            
            @client.event
            async def on_ready():
                try:
                    logger.info(f"[SpaceLord] 🔗 Connected to Discord as {client.user}")
                    
                    # Get the persona channel
                    channel_id = self.config['discord']['persona_channel_id']
                    channel = client.get_channel(channel_id)
                    
                    if not channel:
                        logger.error(f"[SpaceLord] ❌ Could not find Discord channel {channel_id}")
                        return
                    
                    logger.info(f"[SpaceLord] 📖 Fetching persona from Discord channel: {channel.name}")
                    
                    # Fetch recent messages from the persona channel
                    async for message in channel.history(limit=50):
                        if message.content.strip():
                            persona_content.append(message.content)
                    
                    # Reverse to get chronological order
                    persona_content.reverse()
                    
                    logger.info(f"[SpaceLord] ✅ Fetched {len(persona_content)} persona messages from Discord")
                    
                except Exception as e:
                    logger.error(f"[SpaceLord] ❌ Error fetching persona from Discord: {e}")
                finally:
                    await client.close()
            
            # Run the Discord client
            await client.start(self.config['discord']['bot_token'])
            
            if persona_content:
                # Combine all persona messages
                combined_persona = "\n\n".join(persona_content)
                logger.info(f"[SpaceLord] 📝 Combined persona from Discord: {len(combined_persona)} characters")
                return combined_persona
            else:
                logger.warning("[SpaceLord] ⚠️ No persona content found in Discord channel")
                return None
                
        except Exception as e:
            logger.error(f"[SpaceLord] ❌ Error in Discord persona fetch: {e}")
            return None
    
    async def _fetch_memories_from_discord(self):
        """Fetch Space Lord's memories from Discord channel."""
        try:
            if not self.config.get('discord', {}).get('bot_token'):
                logger.warning("[SpaceLord] ⚠️ No Discord bot token configured, using local memories")
                return None
            
            # Create Discord client
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)
            
            memories_content = []
            
            @client.event
            async def on_ready():
                try:
                    logger.info(f"[SpaceLord] 🔗 Connected to Discord for memories as {client.user}")
                    
                    # Get the memories channel
                    channel_id = self.config['discord']['memories_channel_id']
                    channel = client.get_channel(channel_id)
                    
                    if not channel:
                        logger.error(f"[SpaceLord] ❌ Could not find Discord memories channel {channel_id}")
                        return
                    
                    logger.info(f"[SpaceLord] 📖 Fetching memories from Discord channel: {channel.name}")
                    
                    # Fetch recent messages from the memories channel
                    async for message in channel.history(limit=100):
                        if message.content.strip():
                            memories_content.append(message.content)
                    
                    # Reverse to get chronological order
                    memories_content.reverse()
                    
                    logger.info(f"[SpaceLord] ✅ Fetched {len(memories_content)} memory messages from Discord")
                    
                except Exception as e:
                    logger.error(f"[SpaceLord] ❌ Error fetching memories from Discord: {e}")
                finally:
                    await client.close()
            
            # Run the Discord client
            await client.start(self.config['discord']['bot_token'])
            
            if memories_content:
                # Combine all memory messages
                combined_memories = "\n\n".join(memories_content)
                logger.info(f"[SpaceLord] 📝 Combined memories from Discord: {len(combined_memories)} characters")
                return combined_memories
            else:
                logger.warning("[SpaceLord] ⚠️ No memory content found in Discord channel")
                return None
                
        except Exception as e:
            logger.error(f"[SpaceLord] ❌ Error in Discord memories fetch: {e}")
            return None
    
    async def add_memory_to_discord(self, memory_content: str):
        """Add a memory to Space Lord's Discord memories channel."""
        try:
            if not self.config.get('discord', {}).get('bot_token'):
                logger.warning("[SpaceLord] ⚠️ No Discord bot token configured, cannot add memory")
                return False
            
            # Create Discord client
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)
            
            memory_sent = False
            
            @client.event
            async def on_ready():
                try:
                    logger.info(f"[SpaceLord] 🔗 Connected to Discord for memory writing as {client.user}")
                    
                    # Get the memories channel
                    channel_id = self.config['discord']['memories_channel_id']
                    channel = client.get_channel(channel_id)
                    
                    if not channel:
                        logger.error(f"[SpaceLord] ❌ Could not find Discord memories channel {channel_id}")
                        return
                    
                    logger.info(f"[SpaceLord] 📝 Adding memory to Discord channel: {channel.name}")
                    
                    # Send the memory to the channel
                    await channel.send(memory_content)
                    memory_sent = True
                    
                    logger.info(f"[SpaceLord] ✅ Successfully added memory to Discord: {memory_content[:50]}...")
                    
                except Exception as e:
                    logger.error(f"[SpaceLord] ❌ Error adding memory to Discord: {e}")
                finally:
                    await client.close()
            
            # Run the Discord client
            await client.start(self.config['discord']['bot_token'])
            
            return memory_sent
                
        except Exception as e:
            logger.error(f"[SpaceLord] ❌ Error in Discord memory writing: {e}")
            return False
    
    async def should_remember(self, username: str, message: str, response: str = None) -> bool:
        """Determine if Space Lord should remember this interaction."""
        try:
            # Create a context for the memory decision
            context = f"User: {username}\nMessage: {message}"
            if response:
                context += f"\nSpace Lord's Response: {response}"
            
            prompt = f"""Decide whether Space Lord should remember this interaction in his permanent memory.

Space Lord should remember if ANY of these conditions are met:
1. The interaction reveals important information about the user
2. The interaction contains valuable knowledge or insights
3. The interaction establishes a significant relationship or connection
4. The interaction involves important decisions or commitments
5. The interaction contains information that could be useful in future conversations
6. The interaction is emotionally significant or memorable

Context:
{context}

Consider:
- Is this information worth preserving for future reference?
- Would this help Space Lord better understand or interact with this user?
- Is this knowledge that could be valuable in other contexts?

Respond with one word: "yes" or "no"

Answer:"""
            
            # Prepare API request
            system_message = self.persona if self.discord_persona_loaded else "You are Space Lord, an intergalactic overlord and stream moderator. You should remember important interactions and information."
            
            api_messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ]
            
            # Get response from OpenAI
            response = self.openai_client.chat.completions.create(
                model=self.config['openai']['model'],
                messages=api_messages,
                temperature=0.7,
                max_tokens=50
            )
            
            # Extract the response
            response_text = response.choices[0].message.content.strip()
            should_remember = response_text.lower().endswith('yes')
            
            logger.info(f"[SpaceLord] 🤔 Memory decision for '{message[:30]}...': {should_remember}")
            
            return should_remember
            
        except Exception as e:
            _log_space_lord_openai_error("should_remember", e)
            return False
    
    async def create_memory_entry(self, username: str, message: str, response: str = None) -> str:
        """Create a formatted memory entry for Discord."""
        try:
            # Extract key information from the interaction
            key_info = await self.extract_key_information(username, message, response)
            
            if key_info:
                return key_info
            else:
                # Fallback to simple format if extraction fails
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                return f"[{timestamp}] {username}: {message}"
            
        except Exception as e:
            logger.error(f"[SpaceLord] Error creating memory entry: {str(e)}")
            return None
    
    async def extract_key_information(self, username: str, message: str, response: str = None) -> str:
        """Extract key information from an interaction for memory storage."""
        try:
            context = f"User: {username}\nMessage: {message}"
            if response:
                context += f"\nSpace Lord's Response: {response}"
            
            prompt = f"""Extract the key information from this interaction that should be remembered.

Focus on:
1. Important facts about the user (name, occupation, location, preferences, etc.)
2. Significant information shared by the user
3. Important details that would be useful for future conversations
4. Key insights or knowledge mentioned

Context:
{context}

Extract only the essential information in a concise format. 
If the information is about the speaker (the person who sent the message), include their username as a prefix.

Examples:
- "pinnerbob drives a freightliner"
- "pinnerbob is from Texas"
- "pinnerbob likes space exploration"
- "pinnerbob works as a truck driver"
- "pinnerbob has a dog named rover"
- "pinnerbob's favorite color is blue"

If no important information is present, respond with "NO_KEY_INFO"

Extracted information:"""
            
            # Prepare API request
            system_message = self.persona if self.discord_persona_loaded else "You are Space Lord, an intergalactic overlord. Extract only the most important information from interactions."
            
            api_messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ]
            
            # Get response from OpenAI
            response = self.openai_client.chat.completions.create(
                model=self.config['openai']['model'],
                messages=api_messages,
                temperature=0.3,
                max_tokens=100
            )
            
            # Extract the response
            extracted_info = response.choices[0].message.content.strip()
            
            # Check if no key info was found
            if extracted_info.upper() == "NO_KEY_INFO" or not extracted_info:
                return None
            
            logger.info(f"[SpaceLord] 📝 Extracted key info: {extracted_info}")
            return extracted_info
            
        except Exception as e:
            _log_space_lord_openai_error("extract_key_information", e)
            return None
    
    def _save_persona(self, persona):
        """Save Space Lord's persona to file."""
        try:
            with open("space_lord_persona.txt", "w", encoding="utf-8") as f:
                f.write(persona)
            logger.info("[SpaceLord] ✅ Persona saved successfully")
        except Exception as e:
            logger.error(f"[SpaceLord] Error saving persona: {e}")
    
    def add_memory(self, memory):
        """Add a memory to Space Lord's memory bank."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        memory_entry = f"[{timestamp}] {memory}"
        self.memories.append(memory_entry)
        
        # Keep only the most recent memories
        if len(self.memories) > self.max_memories:
            self.memories.pop(0)
        
        # Save memories to file
        try:
            with open("space_lord_memories.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(self.memories))
        except Exception as e:
            logger.error(f"[SpaceLord] Error saving memories: {e}")
    
    def _load_memories(self):
        """Load Space Lord's memories from file."""
        try:
            if os.path.exists("space_lord_memories.txt"):
                with open("space_lord_memories.txt", "r", encoding="utf-8") as f:
                    self.memories = f.read().strip().split("\n") if f.read().strip() else []
        except Exception as e:
            logger.error(f"[SpaceLord] Error loading memories: {e}")
    
    async def should_respond(self, username: str, message: str) -> bool:
        """Determine if Space Lord should respond to a message."""
        try:
            # Check cooldown
            current_time = time.time()
            if current_time - self.last_response_time < self.response_cooldown:
                return False
            
            # Add the message to memories
            self.add_memory(f"{username}: {message}")
            
            # Prepare context for OpenAI with Discord memories
            local_memories = "\n".join(self.memories[-10:])  # Last 10 local memories
            discord_memories = self.discord_memories if self.discord_memories_loaded else ""
            
            # Combine local and Discord memories
            if discord_memories:
                memories_context = f"Discord Memories:\n{discord_memories}\n\nRecent Chat History:\n{local_memories}"
            else:
                memories_context = f"Recent Chat History:\n{local_memories}"
            prompt = f"""Decide whether Space Lord should respond to the following chat message.

IMPORTANT: Space Lord should respond "yes" if ANY of these conditions are met:
1. The message directly addresses Space Lord (e.g., "Hey Space Lord", "Space Lord, who is...")
2. The message mentions Space Lord by name
3. The message asks about space, cosmic power, or Space Lord's domain
4. The message is part of an ongoing conversation that Space Lord is involved in
5. The message challenges Space Lord's authority or power
6. The message is clearly intended for Space Lord based on chat context

Recent Chat History:
{memories_context}

The most recent message is from {username}:
"{message}"

Consider:
- Is this message part of an ongoing conversation?
- Is it clearly directed at Space Lord?
- Would Space Lord have relevant information to share?

Respond with one word: "yes" or "no"

Answer:"""
            
            # Prepare API request with Discord persona
            system_message = self.persona if self.discord_persona_loaded else "You are Space Lord, an intergalactic overlord and stream moderator. You should respond to messages that are directed at you or that you have relevant information about."
            
            api_messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ]
            
            # Log the complete API request
            logger.info("=" * 80)
            logger.info("[Space Lord] 🚀 SHOULD_RESPOND API REQUEST:")
            logger.info("=" * 80)
            logger.info(f"Model: {self.config['openai']['model']}")
            logger.info(f"Temperature: 0.7")
            logger.info(f"Max Tokens: 100")
            logger.info("")
            logger.info("📤 MESSAGES SENT TO API:")
            for i, msg in enumerate(api_messages):
                logger.info(f"Message {i+1} ({msg['role']}):")
                logger.info(f"Content: {msg['content']}")
                logger.info("")
            logger.info("=" * 80)
            
            # Send to GUI
            gui_message = f"🚀 SHOULD_RESPOND API REQUEST:\nModel: {self.config['openai']['model']}\nTemperature: 0.7\nMax Tokens: 100\n\n📤 MESSAGES SENT TO API:\n"
            for i, msg in enumerate(api_messages):
                gui_message += f"Message {i+1} ({msg['role']}):\n{msg['content'][:200]}...\n\n"
            add_gui_message(gui_message, "SHOULD_RESPOND_API")
            
            # Get response from OpenAI
            response = self.openai_client.chat.completions.create(
                model=self.config['openai']['model'],
                messages=api_messages,
                temperature=0.7,
                max_tokens=100
            )
            
            # Extract and log the response
            response_text = response.choices[0].message.content.strip()
            should_respond = response_text.lower().endswith('yes')
            
            # Log the API response
            logger.info("=" * 80)
            logger.info("[Space Lord] 📥 SHOULD_RESPOND API RESPONSE:")
            logger.info("=" * 80)
            logger.info(f"Raw Response: {response_text}")
            logger.info(f"Should Respond: {should_respond}")
            logger.info(f"Usage: {response.usage}")
            logger.info("=" * 80)
            
            # Send to GUI
            gui_message = f"📥 SHOULD_RESPOND API RESPONSE:\nRaw Response: {response_text}\nShould Respond: {should_respond}\nUsage: {response.usage}"
            add_gui_message(gui_message, "SHOULD_RESPOND_API")
            
            # Update last response time if we're going to respond
            if should_respond:
                self.last_response_time = current_time
            
            return should_respond
            
        except Exception as e:
            _log_space_lord_openai_error("should_respond", e)
            return False
    
    async def respond_to_chat(self, username: str, message: str) -> str:
        """Generate a Space Lord response to a chat message."""
        try:
            # Add the message to memories
            self.add_memory(f"{username}: {message}")
            
            # Prepare context for OpenAI with Discord persona and memories
            local_memories = "\n".join(self.memories[-10:])  # Last 10 local memories
            discord_memories = self.discord_memories if self.discord_memories_loaded else ""
            
            # Combine local and Discord memories
            if discord_memories:
                memories_context = f"Discord Memories:\n{discord_memories}\n\nRecent Chat History:\n{local_memories}"
            else:
                memories_context = f"Recent Chat History:\n{local_memories}"
            
            system_message = self.persona if self.discord_persona_loaded else "You are Space Lord, a charismatic and slightly eccentric space explorer and streamer. You have a deep, commanding voice and speak with authority about space, technology, and life. You're friendly but maintain an air of mystery about your cosmic adventures. You respond to Twitch chat messages with wisdom, humor, and occasional space facts. Keep responses under 100 words and maintain your unique personality."
            
            prompt = f"""Persona: {system_message}

{memories_context}

Current Message from {username}: {message}

Respond as Space Lord to this message. Keep your response under 100 words, engaging, and in character. If the message doesn't require a response, return 'NO_RESPONSE'."""

            # Prepare API request with Discord persona
            system_message = self.persona if self.discord_persona_loaded else "You are Space Lord, a charismatic and slightly eccentric space explorer and streamer. You have a deep, commanding voice and speak with authority about space, technology, and life. You're friendly but maintain an air of mystery about your cosmic adventures. You respond to Twitch chat messages with wisdom, humor, and occasional space facts. Keep responses under 100 words and maintain your unique personality."
            
            api_messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ]
            
            # Log the complete API request
            logger.info("=" * 80)
            logger.info("[Space Lord] 🚀 GENERATE_RESPONSE API REQUEST:")
            logger.info("=" * 80)
            logger.info(f"Model: {self.config['openai']['model']}")
            logger.info(f"Temperature: {self.config['openai']['temperature']}")
            logger.info(f"Max Tokens: {self.config['openai']['max_tokens']}")
            logger.info("")
            logger.info("📤 MESSAGES SENT TO API:")
            for i, msg in enumerate(api_messages):
                logger.info(f"Message {i+1} ({msg['role']}):")
                logger.info(f"Content: {msg['content']}")
                logger.info("")
            logger.info("=" * 80)
            
            # Send to GUI
            gui_message = f"🤖 GENERATE_RESPONSE API REQUEST:\nModel: {self.config['openai']['model']}\nTemperature: {self.config['openai']['temperature']}\nMax Tokens: {self.config['openai']['max_tokens']}\n\n📤 MESSAGES SENT TO API:\n"
            for i, msg in enumerate(api_messages):
                gui_message += f"Message {i+1} ({msg['role']}):\n{msg['content'][:200]}...\n\n"
            add_gui_message(gui_message, "GENERATE_RESPONSE_API")

            # Generate response using OpenAI
            response = self.openai_client.chat.completions.create(
                model=self.config['openai']['model'],
                messages=api_messages,
                max_tokens=self.config['openai']['max_tokens'],
                temperature=self.config['openai']['temperature']
            )
            
            response_text = response.choices[0].message.content.strip()
            
            # Log the API response
            logger.info("=" * 80)
            logger.info("[Space Lord] 📥 GENERATE_RESPONSE API RESPONSE:")
            logger.info("=" * 80)
            logger.info(f"Generated Response: {response_text}")
            logger.info(f"Usage: {response.usage}")
            logger.info("=" * 80)
            
            # Send to GUI
            gui_message = f"📥 GENERATE_RESPONSE API RESPONSE:\nGenerated Response: {response_text}\nUsage: {response.usage}"
            add_gui_message(gui_message, "GENERATE_RESPONSE_API")
            
            # Check if response is valid
            if response_text.upper() == "NO_RESPONSE" or not response_text:
                return None
            
            # Check if this interaction should be remembered
            try:
                should_remember = await self.should_remember(username, message, response_text)
                if should_remember:
                    logger.info(f"[SpaceLord] 💾 Interaction deemed memorable, adding to Discord memories...")
                    memory_entry = await self.create_memory_entry(username, message, response_text)
                    if memory_entry:
                        success = await self.add_memory_to_discord(memory_entry)
                        if success:
                            logger.info(f"[SpaceLord] ✅ Successfully added memory to Discord")
                        else:
                            logger.warning(f"[SpaceLord] ⚠️ Failed to add memory to Discord")
            except Exception as e:
                logger.error(f"[SpaceLord] ❌ Error in memory processing: {e}")
            
            # Update last response time
            self.last_response_time = time.time()
            
            logger.info(f"[SpaceLord] 🤖 Generated response: {response_text}")
            return response_text
            
        except Exception as e:
            _log_space_lord_openai_error("respond_to_chat", e)
            return None
    
    def update_persona(self, new_persona: str):
        """Update Space Lord's persona."""
        self.persona = new_persona
        self._save_persona(new_persona)
        logger.info("[SpaceLord] ✅ Persona updated successfully")
    
    def get_memories(self):
        """Get Space Lord's recent memories."""
        return self.memories[-10:]  # Return last 10 memories
    
    def start_voice_listener(self):
        """Start listening for voice wake words."""
        try:
            if self.is_listening:
                logger.info("[SpaceLord] 🎤 Voice listener already running")
                return
            
            self.is_listening = True
            self.voice_listener = VoiceListener(self)
            self.voice_listener.start()
            logger.info("[SpaceLord] 🎤 Voice listener started successfully")
            
            # Send test message to GUI to verify it's working
            try:
                self._send_to_gui("🎤 Voice listener started and listening for wake words", "VOICE_LISTENER")
            except Exception as gui_error:
                logger.error(f"[SpaceLord] ❌ GUI error: {gui_error}")
            
        except Exception as e:
            logger.error(f"[SpaceLord] ❌ Error starting voice listener: {e}")
            self.is_listening = False
    
    def stop_voice_listener(self):
        """Stop listening for voice wake words."""
        try:
            if self.voice_listener:
                self.voice_listener.stop()
                self.voice_listener = None
            self.is_listening = False
            logger.info("[SpaceLord] 🎤 Voice listener stopped")
            
        except Exception as e:
            logger.error(f"[SpaceLord] ❌ Error stopping voice listener: {e}")
    
    def _send_to_gui(self, message: str, message_type: str = "VOICE_LISTENER"):
        """Send message to GUI if available."""
        try:
            # Try to use the main bot's GUI system
            from gui_monitor import add_gui_message
            add_gui_message(message, message_type)
            logger.debug(f"[SpaceLord] ✅ Sent to GUI: {message}")
        except ImportError:
            # GUI not available, just log
            logger.debug(f"[SpaceLord] GUI not available: {message}")
        except Exception as e:
            logger.error(f"[SpaceLord] Error sending to GUI: {e}")
            # Fallback: just log to console
            logger.info(f"[SpaceLord] {message}")


class VoiceListener:
    """Listens for voice wake words and processes voice commands."""
    
    def __init__(self, space_lord):
        self.space_lord = space_lord
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        self.is_listening = False
        self.audio_queue = queue.Queue()
        self.wake_words = ["hey mudflap", "hey space lord", "mudflap", "space lord"]
        
        # Adjust for ambient noise
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1)
            logger.info("[VoiceListener] 🎤 Microphone calibrated for ambient noise")
    
    def start(self):
        """Start listening for wake words."""
        self.is_listening = True
        self.listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.listen_thread.start()
        logger.info("[VoiceListener] 🎤 Started listening for wake words")
        
        # Send test message to GUI
        self.space_lord._send_to_gui("🎤 Voice listener thread started and listening", "VOICE_LISTENER")
        
        # Simple microphone test - just basic access check
        try:
            logger.info("[VoiceListener] 🧪 Testing microphone access...")
            logger.info(f"[VoiceListener] 🧪 Microphone object: {self.microphone}")
            logger.info(f"[VoiceListener] 🧪 Microphone type: {type(self.microphone)}")
            
            # Just check if we can access the microphone object
            logger.info("[VoiceListener] 🧪 Microphone object accessible")
            
            # Try to get basic info without opening the source
            try:
                logger.info(f"[VoiceListener] 🧪 Microphone device index: {self.microphone.device_index}")
                logger.info(f"[VoiceListener] 🧪 Microphone list: {sr.Microphone.list_microphone_names()}")
            except Exception as info_error:
                logger.warning(f"[VoiceListener] ⚠️ Could not get microphone info: {info_error}")
            
            logger.info("[VoiceListener] 🧪 Basic microphone test successful")
                
        except Exception as e:
            logger.error(f"[VoiceListener] ❌ Microphone test failed: {e}")
            import traceback
            logger.error(f"[VoiceListener] ❌ Microphone test traceback: {traceback.format_exc()}")
            logger.warning("[VoiceListener] ⚠️ Continuing without microphone test")
    
    def stop(self):
        """Stop listening for wake words."""
        self.is_listening = False
        logger.info("[VoiceListener] 🎤 Stopped listening for wake words")
    
    def _listen_loop(self):
        """Main listening loop for wake word detection."""
        import threading
        import traceback
        thread_id = threading.current_thread().ident
        loop_count = 0
        logger.info(f"[VoiceListener-{thread_id}] 🎤 Starting main listening loop in thread...")
        logger.info(f"[VoiceListener-{thread_id}] 🎤 Thread info: {threading.current_thread().name}")
        
        while self.is_listening:
            try:
                logger.debug(f"[VoiceListener-{thread_id}] 🎤 Loop {loop_count}: Starting audio capture...")
                
                # Simple, safe listening without complex error handling
                with self.microphone as source:
                    logger.debug(f"[VoiceListener-{thread_id}] 🎤 Microphone source acquired, starting listen...")
                    audio = self.recognizer.listen(source, timeout=1, phrase_time_limit=5)
                    logger.info(f"[VoiceListener-{thread_id}] 🎤 Audio captured successfully, processing...")
                    
                    # Process the audio
                    logger.debug(f"[VoiceListener-{thread_id}] 🎤 Calling _process_audio...")
                    self._process_audio(audio)
                    logger.debug(f"[VoiceListener-{thread_id}] 🎤 _process_audio completed successfully")
                    
            except sr.WaitTimeoutError:
                # No speech detected, continue listening
                loop_count += 1
                logger.debug(f"[VoiceListener-{thread_id}] 🎤 No speech detected (loop {loop_count})")
                continue
            except Exception as e:
                loop_count += 1
                logger.error(f"[VoiceListener-{thread_id}] ❌ CRITICAL ERROR in listening loop {loop_count}: {e}")
                logger.error(f"[VoiceListener-{thread_id}] ❌ Error type: {type(e).__name__}")
                logger.error(f"[VoiceListener-{thread_id}] ❌ Full traceback:")
                logger.error(f"[VoiceListener-{thread_id}] {traceback.format_exc()}")
                
                # Try to continue, but log the error
                try:
                    logger.info(f"[VoiceListener-{thread_id}] 🎤 Attempting to continue after error...")
                    continue
                except Exception as recovery_error:
                    logger.error(f"[VoiceListener-{thread_id}] ❌ CRASH: Cannot recover from error: {recovery_error}")
                    break
    
    def _process_audio(self, audio):
        """Process captured audio for wake word detection."""
        import threading
        import traceback
        thread_id = threading.current_thread().ident
        logger.debug(f"[VoiceListener-{thread_id}] 🎤 Starting _process_audio...")
        
        try:
            logger.debug(f"[VoiceListener-{thread_id}] 🎤 Audio object type: {type(audio)}")
            logger.debug(f"[VoiceListener-{thread_id}] 🎤 Calling recognize_google...")
            
            # Convert speech to text
            text = self.recognizer.recognize_google(audio).lower()
            logger.info(f"[VoiceListener-{thread_id}] 🎤 Heard: {text}")
            
            # Send to GUI if available (safely)
            logger.debug(f"[VoiceListener-{thread_id}] 🎤 Attempting to send to GUI...")
            try:
                self.space_lord._send_to_gui(f"🎤 Heard: {text}", "VOICE_LISTENER")
                logger.debug(f"[VoiceListener-{thread_id}] 🎤 GUI update successful")
            except Exception as gui_error:
                logger.error(f"[VoiceListener-{thread_id}] ❌ GUI error: {gui_error}")
            
            # Check for wake words
            logger.debug(f"[VoiceListener-{thread_id}] 🎤 Checking for wake words...")
            if any(wake_word in text for wake_word in self.wake_words):
                logger.info(f"[VoiceListener-{thread_id}] 🚨 WAKE WORD DETECTED: {text}")
                try:
                    self.space_lord._send_to_gui(f"🚨 WAKE WORD DETECTED: {text}", "VOICE_LISTENER")
                except Exception as gui_error:
                    logger.error(f"[VoiceListener-{thread_id}] ❌ GUI error: {gui_error}")
                
                logger.debug(f"[VoiceListener-{thread_id}] 🎤 Calling _handle_wake_word...")
                self._handle_wake_word(text)
                logger.debug(f"[VoiceListener-{thread_id}] 🎤 _handle_wake_word completed")
            else:
                logger.info(f"[VoiceListener-{thread_id}] 🎤 No wake word in: {text}")
                try:
                    self.space_lord._send_to_gui(f"👂 No wake word detected", "VOICE_LISTENER")
                except Exception as gui_error:
                    logger.error(f"[VoiceListener-{thread_id}] ❌ GUI error: {gui_error}")
                
        except sr.UnknownValueError:
            # Speech was unintelligible
            logger.info(f"[VoiceListener-{thread_id}] 🎤 Speech unintelligible")
        except sr.RequestError as e:
            logger.error(f"[VoiceListener-{thread_id}] ❌ Speech recognition service error: {e}")
            logger.error(f"[VoiceListener-{thread_id}] ❌ RequestError traceback: {traceback.format_exc()}")
        except Exception as e:
            logger.error(f"[VoiceListener-{thread_id}] ❌ CRITICAL ERROR processing audio: {e}")
            logger.error(f"[VoiceListener-{thread_id}] ❌ Error type: {type(e).__name__}")
            logger.error(f"[VoiceListener-{thread_id}] ❌ Full traceback:")
            logger.error(f"[VoiceListener-{thread_id}] {traceback.format_exc()}")
        
        logger.debug(f"[VoiceListener-{thread_id}] 🎤 _process_audio completed")
    
    def _handle_wake_word(self, text):
        """Handle detected wake word and get voice command."""
        import threading
        import traceback
        thread_id = threading.current_thread().ident
        logger.debug(f"[VoiceListener-{thread_id}] 🚨 Starting _handle_wake_word...")
        
        try:
            logger.info(f"[VoiceListener-{thread_id}] 🚨 Processing wake word: {text}")
            
            # Speak acknowledgment
            if "mudflap" in text.lower():
                response = "Hey there! What can I do for you?"
            else:
                response = "Space Lord here! What do you need?"
            
            # Log the response instead of trying to speak (avoid async issues in thread)
            logger.info(f"[VoiceListener-{thread_id}] 🗣️ Would say: {response}")
            logger.debug(f"[VoiceListener-{thread_id}] 🎤 Attempting to send response to GUI...")
            
            try:
                self.space_lord._send_to_gui(f"🗣️ Wake word response: {response}", "VOICE_LISTENER")
                logger.debug(f"[VoiceListener-{thread_id}] 🎤 GUI response update successful")
            except Exception as gui_error:
                logger.error(f"[VoiceListener-{thread_id}] ❌ GUI error: {gui_error}")
            
            # Listen for the command
            logger.debug(f"[VoiceListener-{thread_id}] 🎤 Calling _listen_for_command...")
            self._listen_for_command()
            logger.debug(f"[VoiceListener-{thread_id}] 🎤 _listen_for_command completed")
            
        except Exception as e:
            logger.error(f"[VoiceListener-{thread_id}] ❌ CRITICAL ERROR handling wake word: {e}")
            logger.error(f"[VoiceListener-{thread_id}] ❌ Error type: {type(e).__name__}")
            logger.error(f"[VoiceListener-{thread_id}] ❌ Full traceback:")
            logger.error(f"[VoiceListener-{thread_id}] {traceback.format_exc()}")
        
        logger.debug(f"[VoiceListener-{thread_id}] 🚨 _handle_wake_word completed")
    
    def _listen_for_command(self):
        """Listen for the voice command after wake word."""
        try:
            logger.info("[VoiceListener] 🎤 Listening for command...")
            
            with self.microphone as source:
                audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
                command = self.recognizer.recognize_google(audio).lower()
                
                logger.info(f"[VoiceListener] 🎤 Command received: {command}")
                self._process_command(command)
                
        except sr.WaitTimeoutError:
            logger.info("[VoiceListener] ⏰ No command received, returning to wake word listening")
        except sr.UnknownValueError:
            logger.info("[VoiceListener] 🎤 Command was unintelligible")
        except Exception as e:
            logger.error(f"[VoiceListener] ❌ Error listening for command: {e}")
    
    def _process_command(self, command):
        """Process the voice command."""
        try:
            logger.info(f"[VoiceListener] 🎤 Processing command: {command}")
            
            # Simple command processing - can be expanded
            if "stop listening" in command or "stop" in command:
                logger.info("[VoiceListener] 🛑 Stop command received")
                response = "Stopping voice listening. Say 'hey mudflap' or 'hey space lord' to wake me up again."
                logger.info(f"[VoiceListener] 🗣️ Would say: {response}")
                self.space_lord._send_to_gui(f"🛑 Stop command: {response}", "VOICE_LISTENER")
                self.stop()
            elif "hello" in command or "hi" in command:
                response = "Hello! Nice to hear from you!"
                logger.info(f"[VoiceListener] 🗣️ Would say: {response}")
                self.space_lord._send_to_gui(f"👋 Greeting: {response}", "VOICE_LISTENER")
            else:
                response = f"I heard you say: {command}. I'm still learning voice commands, but I'm listening!"
                logger.info(f"[VoiceListener] 🗣️ Would say: {response}")
                self.space_lord._send_to_gui(f"💬 Command response: {response}", "VOICE_LISTENER")
                
        except Exception as e:
            logger.error(f"[VoiceListener] ❌ Error processing command: {e}")
    
class TwitchBot(twitch_commands.Bot):
    """Twitch bot that reads chat messages aloud through Bluetooth."""
    
    def __init__(self, config, tts_system, *, config_path: str | os.PathLike[str] = "config.yaml"):
        # Handle token format - try both with and without oauth: prefix
        token = config['twitch']['bot_token']
        original_token = token
        
        # Remove oauth: prefix if present for TwitchIO
        if token.startswith('oauth:'):
            token = token[6:]  # Remove oauth: prefix
            logger.info(f"[Twitch] 🔍 Removed oauth: prefix from token")
        
        logger.info(f"[Twitch] 🔍 Original token: {original_token[:20]}...")
        logger.info(f"[Twitch] 🔍 Processed token: {token[:20]}...")
        logger.info(f"[Twitch] 🔍 Using client_id: {config['twitch']['client_id']}")
        logger.info(f"[Twitch] 🔍 Bot account (login): {config['twitch']['bot_username']}")
        logger.info(f"[Twitch] 🔍 Chat channel login: {config['twitch']['channel']}")

        self._oauth_access_token = token
        refresh = config['twitch'].get('refresh_token') or ''
        rt_len = len(refresh.strip()) if refresh else 0
        logger.info(
            "[Twitch][debug] refresh_token set=%s (length=%s, never logged)",
            bool(rt_len),
            rt_len,
        )

        # Initialize with all required parameters
        try:
            super().__init__(
                client_id=config['twitch']['client_id'],
                client_secret=config['twitch']['client_secret'],
                prefix='!',
                bot_id=str(config['twitch']['bot_id']),
            )
            logger.info(f"[Twitch] ✅ Initialized with all required parameters")
        except Exception as e:
            logger.error(f"[Twitch] ❌ Failed to initialize: {e}")
            import traceback
            logger.error(f"[Twitch] Traceback: {traceback.format_exc()}")
            raise Exception(f"Could not initialize Twitch bot: {e}")
        self.config = config
        self.config_path = os.path.abspath(str(config_path))
        self.tts_system = tts_system
        self.chat_reading_enabled = config['twitch'].get('always_read_chat', True)
        self.space_lord = SpaceLord(config, tts_system)
        logger.info("[Twitch] ✅ Space Lord AI initialized")
        self._oauth_refresh_token = refresh
        self._eventsub_chat_ready = False
        self._discord_listen_proc: multiprocessing.Process | None = None
        self._discord_tx_proc: multiprocessing.Process | None = None
        self._discord_pcm_queue: Any = None
        logger.info("[Twitch][debug] twitchio version=%s", getattr(twitchio, "__version__", "?"))

    def _twitch_verbose_debug(self) -> bool:
        """Extra message-level logs — set twitch.verbose_debug or debug.enabled in config.yaml."""
        tw = self.config.get('twitch', {})
        dbg = self.config.get('debug', {})
        return bool(tw.get('verbose_debug')) or bool(dbg.get('enabled'))

    def _tw_http_exc_extras(self, e: BaseException) -> str:
        """Collect HTTPException-ish fields without tokens."""
        chunks: list[str] = []
        for name in ('status', 'code', 'message', 'reason', 'detail', 'body', 'text', 'payload'):
            raw = getattr(e, name, None)
            if raw is None:
                continue
            s = str(raw).strip()
            if not s:
                continue
            if len(s) > 1200:
                s = s[:1200] + '...(trunc)'
            chunks.append(f"{name}={s}")
        return ' | '.join(chunks) if chunks else repr(e)

    async def login(self, *, token: str | None = None, load_tokens: bool = True, save_tokens: bool = True):
        """Run TwitchIO login; if setup fails, clear _login_called so callers can retry a full login."""
        try:
            logger.info(
                "[Twitch][debug] login(): load_tokens=%s save_tokens=%s token_kwarg=%s",
                load_tokens,
                save_tokens,
                token is not None,
            )
            await super().login(token=token, load_tokens=load_tokens, save_tokens=save_tokens)
            logger.info("[Twitch][debug] login(): completed OK")
        except BaseException:
            logger.warning("[Twitch][debug] login(): failed — clearing _login_called / _setup_called for retry")
            self._login_called = False
            self._setup_called = False
            raise

    async def setup_hook(self):
        """Register the bot OAuth token and subscribe to EventSub chat (TwitchIO 3 — no IRC)."""
        self._eventsub_chat_ready = False
        tw = self.config['twitch']
        logger.info("[Twitch][debug] twitch.verbose_debug or debug.enabled → %s", self._twitch_verbose_debug())

        val = await asyncio.to_thread(_twitch_oauth_validate_sync, self._oauth_access_token)
        if isinstance(val, dict) and val:
            logger.info(
                "[Twitch][debug] user token validate → login=%r user_id=%s expires_in=%s scopes=%s",
                val.get("login"),
                val.get("user_id"),
                val.get("expires_in"),
                val.get("scopes"),
            )
            cid_tok = val.get("client_id")
            cid_cfg = str(tw.get("client_id", "")).strip()
            if cid_tok and cid_cfg and cid_tok != cid_cfg:
                logger.warning(
                    "[Twitch][debug] token Client-ID (%s) != config twitch.client_id (%s): "
                    "OAuth for user tokens must use the same app as in config.",
                    cid_tok,
                    cid_cfg,
                )
            uid_tok = val.get("user_id")
            bid_cfg = str(tw.get("bot_id", "")).strip()
            if uid_tok and bid_cfg and str(uid_tok) != bid_cfg:
                logger.warning(
                    "[Twitch][debug] token user_id=%s != config bot_id=%s "
                    "(set bot_id to the validate user_id for this bot_token).",
                    uid_tok,
                    bid_cfg,
                )
            name_cfg = str(tw.get("bot_username", "")).strip().lstrip("#").lower()
            login_tok = (val.get("login") or "").lower()
            if name_cfg and login_tok and name_cfg != login_tok:
                logger.warning(
                    "[Twitch][debug] bot_username=%r vs token login=%r (prefer token login spelling).",
                    tw.get("bot_username"),
                    val.get("login"),
                )
            if val.get("user_id") is None and val.get("login") is None:
                logger.warning(
                    "[Twitch][debug] validate has no login/user_id — looks like an app token, "
                    "not a user OAuth token (use authorization_code grant for twitch.bot_token)."
                )
        else:
            logger.warning(
                "[Twitch][debug] validate failed or empty — bot_token may be invalid, revoked, "
                "or wrong type."
            )

        try:
            await self.add_token(self._oauth_access_token, self._oauth_refresh_token)
            logger.info("[Twitch] Bot user OAuth token registered for EventSub websocket")
        except InvalidTokenException as e:
            logger.error(
                "[Twitch] Bot OAuth token invalid or expired and could not be refreshed. "
                "Generate a new user token for the bot account (scope user:read:chat) in the Twitch dev console, "
                "and add refresh_token beside bot_token in config.yaml when possible."
            )
            raise
        except Exception as e:
            logger.error(
                "[Twitch] Could not register bot OAuth token (%s). "
                "Ensure bot_token is a valid user access token from your Twitch app.",
                e,
            )
            raise

        channel_login = str(tw['channel']).lstrip('#').strip().lower()
        if tw.get('broadcaster_user_id'):
            broadcaster_id = str(tw['broadcaster_user_id']).strip()
            logger.info("[Twitch][debug] broadcaster from config broadcaster_user_id=%s", broadcaster_id)
        else:
            users = await self.fetch_users(logins=[channel_login])
            if not users:
                raise RuntimeError(
                    f"[Twitch] Unknown channel login {channel_login!r}; "
                    f"fix twitch.channel or set twitch.broadcaster_user_id in config.yaml."
                )
            broadcaster_id = str(users[0].id)
            bc = users[0]
            logger.info(
                "[Twitch][debug] Resolved channel_login=%r → broadcaster_id=%s login=%s display=%s",
                channel_login,
                broadcaster_id,
                getattr(bc, 'name', None),
                getattr(bc, 'display_name', None),
            )

        # Helix sanity check on bot_username ↔ bot_id ↔ token (catches typo / wrong Twitch account).
        bot_login_chk = str(tw.get('bot_username', '')).strip().lstrip('#').lower()
        if bot_login_chk:
            try:
                bot_rows = await self.fetch_users(logins=[bot_login_chk])
                if bot_rows:
                    helix_uid = str(bot_rows[0].id)
                    logger.info(
                        "[Twitch][debug] Helix fetch_users(login=%r) → id=%s display=%s",
                        bot_login_chk,
                        helix_uid,
                        getattr(bot_rows[0], 'display_name', None),
                    )
                    if helix_uid != str(self.bot_id):
                        logger.warning(
                            "[Twitch][debug] twitch.bot_id=%s but Helix says login %r has id=%s (fix bot_id)",
                            self.bot_id,
                            bot_login_chk,
                            helix_uid,
                        )
                    tk_uid = val.get('user_id') if isinstance(val, dict) else None
                    if tk_uid is not None and str(tk_uid) != helix_uid:
                        logger.warning(
                            "[Twitch][debug] bot_token validate user_id=%s vs Helix(bot_username=%r) id=%s",
                            tk_uid,
                            bot_login_chk,
                            helix_uid,
                        )
                else:
                    logger.warning("[Twitch][debug] Helix fetch_users(%r) returned no users.", bot_login_chk)
            except Exception as ex:
                logger.warning("[Twitch][debug] Helix bot sanity check failed: %s", ex)

        logger.info(
            "[Twitch][debug] subscribing channel.chat.message: broadcaster_user_id=%s "
            "listener user_id=%s (Twitch Bot.bot_id) as_bot=%s",
            broadcaster_id,
            self.bot_id,
            True,
        )
        sub = eventsub.ChatMessageSubscription(broadcaster_user_id=broadcaster_id, user_id=str(self.bot_id))
        try:
            sub_result = await self.subscribe_websocket(sub, as_bot=True)
            sub_id = getattr(sub_result, 'id', None) or getattr(sub_result, 'subscription_id', None)
            logger.info(
                "[Twitch][debug] subscribe_websocket OK; result_type=%s subscription_id=%s",
                type(sub_result).__name__,
                sub_id,
            )
            logger.info(
                "[Twitch] EventSub subscription active for channel.chat.message "
                "(broadcaster_id=%s, bot_user_id=%s)",
                broadcaster_id,
                self.bot_id,
            )
        except TwitchHTTPException as e:
            logger.error("[Twitch][debug] TwitchHTTPException fields: %s", self._tw_http_exc_extras(e))
            logger.error(
                "[Twitch][debug] 403 checklist: user token scopes must include user:read:chat; "
                "%s must /mod bot %s (or channel bot authorize); bot_id/token user_id must align; "
                "token Client-ID must match config client_id.",
                channel_login,
                val.get("login") if isinstance(val, dict) else "?",
            )
            logger.error(
                "[Twitch] EventSub chat subscribe failed (HTTP %s): %s. "
                "The bot OAuth needs scope user:read:chat (and broadcaster must mod the bot or grant channel:bot). "
                "If the token rotates, add refresh_token next to bot_token in config.yaml.",
                getattr(e, "status", "?"),
                e,
            )
            raise

        self._eventsub_chat_ready = True
    
    async def event_ready(self):
        """Called when Twitch bot is ready."""
        bot_username = self.config['twitch']['bot_username']
        if not getattr(self, "_eventsub_chat_ready", False):
            logger.warning(
                "[Twitch] EventSub chat subscription did not complete; you will not receive chat until "
                "bot_token (and refresh_token) are valid — see errors above. Retries may show this if login was skipped."
            )
        logger.info(f"[Twitch] ✅ Bot is ready: {bot_username}")
        logger.info(f"[Twitch] ✅ Connected to channel: {self.config['twitch']['channel']}")
        logger.info("[Twitch] 🔄 Bot is running and reading chat messages...")
        logger.info(f"[Twitch] 🔍 Bot will read messages from: {self.config['twitch']['channel']}")
        logger.info(f"[Twitch] 🎤 TTS system ready: {self.tts_system is not None}")
        
        # Test: Try to send a message to verify we can write to the channel
        try:
            channel_name = self.config['twitch']['channel']
            logger.info(f"[Twitch] 🧪 Testing channel write access...")
            # This will help verify the bot has proper permissions
            logger.info(f"[Twitch] 🧪 Bot should be able to read from: {channel_name}")
            
            # Try to send a test message to verify the bot is working
            logger.info(f"[Twitch] 🧪 Attempting to send test message...")
            
            # Test if we can access the channel object
            try:
                if hasattr(self, 'get_channel'):
                    channel = self.get_channel(channel_name)
                    if channel:
                        logger.info(f"[Twitch] ✅ Successfully got channel object: {channel}")
                        logger.info(f"[Twitch] 🔍 Channel name: {getattr(channel, 'name', 'Unknown')}")
                        logger.info(f"[Twitch] 🔍 Channel attributes: {[attr for attr in dir(channel) if not attr.startswith('_')]}")
                    else:
                        logger.warning(f"[Twitch] ⚠️ Could not get channel object for: {channel_name}")
                else:
                    logger.warning(f"[Twitch] ⚠️ No get_channel method available")
            except Exception as e:
                logger.error(f"[Twitch] Error getting channel object: {e}")
            
        except Exception as e:
            logger.error(f"[Twitch] Error in channel test: {e}")
        
        # Initialize Discord persona for Space Lord
        try:
            logger.info("[Twitch] 🔄 Initializing Space Lord Discord persona...")
            await self.space_lord.initialize_discord_persona()
            logger.info("[Twitch] ✅ Space Lord Discord persona initialized")
        except Exception as e:
            logger.error(f"[Twitch] ❌ Error initializing Discord persona: {e}")
            logger.info("[Twitch] ℹ️ Space Lord will use local persona")

        # Discord VC listen + Whisper transcribe = separate OS processes (Queue IPC), after persona fetch.
        try:
            dcfg = self.config.get("discord_voice_transcribe") or {}
            if dcfg.get("enabled"):
                from discord_voice_listen_process import listen_process_entry
                from discord_transcribe_process import transcribe_process_entry

                ctx = multiprocessing.get_context("spawn")
                q = ctx.Queue(maxsize=32)
                self._discord_pcm_queue = q
                self._discord_tx_proc = ctx.Process(
                    target=transcribe_process_entry,
                    args=(self.config_path, q),
                    name="discord-transcribe",
                )
                self._discord_listen_proc = ctx.Process(
                    target=listen_process_entry,
                    args=(self.config_path, q),
                    name="discord-listen",
                )
                self._discord_tx_proc.start()
                self._discord_listen_proc.start()
                logger.info(
                    "[Twitch] 🎙️ Discord voice: transcribe worker PID=%s, listen worker PID=%s (main process = Twitch/TTS)",
                    self._discord_tx_proc.pid,
                    self._discord_listen_proc.pid,
                )
        except Exception as e:
            logger.error("[Twitch] ❌ Could not start Discord voice workers: %s", e)
        
        # Voice listener disabled - focusing on Twitch chat only
        logger.info("[Twitch] 🎤 Voice listener disabled - focusing on Twitch chat reading")
        
        # Verify we're actually in the channel
        try:
            channels = getattr(self, '_ws', None)
            if channels:
                logger.info(f"[Twitch] 🔍 WebSocket channels: {channels}")
            
            # Try to get connected channels
            if hasattr(self, '_connection'):
                logger.info(f"[Twitch] 🔍 Connection object exists")
                if hasattr(self._connection, '_channel_cache'):
                    logger.info(f"[Twitch] 🔍 Channel cache: {self._connection._channel_cache}")
        except Exception as e:
            logger.warning(f"[Twitch] Could not verify channel connection: {e}")
        
        # Send to GUI if available
        if GUI_AVAILABLE:
            add_gui_message(f"✅ Twitch bot connected as {bot_username}", "INFO")
            add_gui_message(f"📺 Reading chat from: {self.config['twitch']['channel']}", "INFO")
            add_gui_message("🎤 TTS system ready and waiting for messages", "INFO")
            add_gui_message("⏳ Waiting for Twitch messages...", "INFO")
    
    async def event_join(self, channel, user):
        """Called when someone joins the channel."""
        logger.info(f"[Twitch] 👋 {user.name} joined {channel.name}")
        
        # Check if it's the bot itself joining
        if user.name.lower() == self.config['twitch']['bot_username'].lower():
            logger.info(f"[Twitch] ✅ Bot {user.name} successfully joined channel {channel.name}")
            if GUI_AVAILABLE:
                add_gui_message(f"✅ Bot joined channel: {channel.name}", "INFO")
    
    async def event_part(self, channel, user):
        """Called when someone leaves the channel."""
        logger.info(f"[Twitch] 👋 {user.name} left {channel.name}")
    
    async def event_connected(self):
        """Called when the bot connects to Twitch."""
        logger.info("[Twitch] 🔌 Bot connected to Twitch servers")
        logger.info(
            "[Twitch][debug] event_connected at=%s eventsub_chat_ready=%s",
            time.strftime('%H:%M:%S'),
            getattr(self, '_eventsub_chat_ready', False),
        )

    async def event_disconnected(self):
        """Called when the bot disconnects from Twitch."""
        logger.warning("[Twitch] 🔌 Bot disconnected from Twitch servers")
        logger.warning("[Twitch][debug] event_disconnected at=%s", time.strftime('%H:%M:%S'))
    
    async def event_message(self, message):
        """Handle incoming Twitch chat messages (EventSub ``ChatMessage``)."""
        try:
            chatter = getattr(message, "chatter", None)
            text = getattr(message, "text", None)
            if chatter is None or text is None:
                logger.debug("[Twitch] Ignoring non-EventSub message payload: %s", type(message))
                return

            if self._twitch_verbose_debug():
                mid = getattr(message, 'id', None)
                bobj = getattr(message, 'broadcaster', None)
                logger.info(
                    "[Twitch][debug] event_message raw id=%s chatter_id=%s broad=%s text_len=%s",
                    mid,
                    getattr(chatter, 'id', None),
                    getattr(bobj, 'name', None),
                    len(text or ''),
                )

            if str(chatter.id) == str(self.bot_id):
                return
            if getattr(message, "source_broadcaster", None) is not None:
                if self._twitch_verbose_debug():
                    logger.info("[Twitch][debug] skip: shared-chat / source_broadcaster set")
                return

            target = self.config['twitch']['channel'].lstrip('#').strip().lower()
            if message.broadcaster.name.lower() != target:
                logger.debug("[Twitch] Skipping message for other broadcaster: %s", message.broadcaster.name)
                return

            logger.info("[Twitch] 📨 Received message from %s: %s", chatter.name, text)

            try:
                with open("twitch_messages.log", "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {chatter.name}: {text}\n")
            except Exception as log_error:
                logger.error("[Twitch] Error logging message to file: %s", log_error)

            if not self.chat_reading_enabled:
                logger.info("[Twitch] Chat reading is disabled")
                return

            if chatter.name.lower() == self.config['twitch']['bot_username'].lower():
                logger.info("[Twitch] Ignoring message from bot login: %s", chatter.name)
                return

            logger.info("[Twitch] Processing message from %s", chatter.name)
            await self.read_chat_message(chatter.name, text)
            if self.config.get('twitch', {}).get('space_lord_enabled', True):
                await self.handle_space_lord_response(chatter.name, text)

            await self.process_commands(message)

        except Exception as e:
            logger.error("[Twitch] Error handling message: %s", e)
            chatter = getattr(message, "chatter", None)
            text = getattr(message, "text", None)
            uname = getattr(chatter, "name", "?")
            logger.error("[Twitch] Message details — user: %s, text: %s", uname, text)
            import traceback
            logger.error("[Twitch] Traceback: %s", traceback.format_exc())
    
    async def read_chat_message(self, username: str, message: str):
        """Read a Twitch chat message aloud using Bluetooth TTS."""
        try:
            logger.info(f"[Twitch] 🎯 Starting to read chat message from {username}")
            
            # Format the message for TTS
            if len(username) > 15:
                username = username[:15]
            
            tts_text = f"{username} says: {message}"
            logger.info(f"[Twitch] 🎤 TTS text: {tts_text}")
            
            # Speak it aloud through Bluetooth
            logger.info(f"[Twitch] 🎤 Calling TTS system to speak...")
            success = await self.tts_system.speak(tts_text)
            logger.info(f"[Twitch] 🎤 TTS result: {success}")
            
            if success:
                logger.info(f"[Twitch] 🔊 Queued message from {username}: {message}")
                # Send to GUI if available
                if GUI_AVAILABLE:
                    add_gui_message(f"Twitch: {username} says: {message}", "INFO")
            else:
                logger.error(f"[Twitch] ❌ Failed to queue message from {username}")
                # Send error to GUI if available
                if GUI_AVAILABLE:
                    add_gui_message(f"Failed to queue message from {username}", "ERROR")
                
        except Exception as e:
            logger.error(f"[Twitch] Error reading chat message: {e}")
            import traceback
            logger.error(f"[Twitch] Traceback: {traceback.format_exc()}")
    
    async def handle_space_lord_response(self, username: str, message: str):
        """Handle Space Lord's response to chat messages."""
        try:
            should_respond = await self.space_lord.should_respond(username, message)
            
            if not should_respond:
                logger.debug(f"[SpaceLord] 🚫 Not responding to {username}: {message}")
                return
            
            # Generate Space Lord response
            response = await self.space_lord.respond_to_chat(username, message)
            
            if response:
                logger.info(f"[SpaceLord] 🚀 Responding to {username}: {response}")
                
                # Speak the response using Space Lord's male voice
                space_lord_text = f"Space Lord says: {response}"
                success = await self._speak_with_male_voice(space_lord_text)
                
                if success:
                    logger.info(f"[SpaceLord] 🔊 Queued Space Lord response: {response}")
                    # Send to GUI if available
                    if GUI_AVAILABLE:
                        add_gui_message(f"🚀 Space Lord: {response}", "SPACE_LORD")
                else:
                    logger.error(f"[SpaceLord] ❌ Failed to queue Space Lord response")
                    
        except Exception as e:
            logger.error(f"[SpaceLord] Error handling response: {e}")
            import traceback
            logger.error(f"[SpaceLord] Traceback: {traceback.format_exc()}")
    
    async def _speak_with_male_voice(self, text: str) -> bool:
        """Speak text using a male voice for Space Lord."""
        try:
            if WINDOWS_TTS_AVAILABLE:
                # Create temporary file
                temp_file = f"temp_space_lord_{int(time.time())}_{hash(text) % 10000}.wav"
                
                # Use Windows SAPI TTS
                speaker = win32com.client.Dispatch("SAPI.SpVoice")
                
                male_voice = await self._find_best_male_voice(speaker)

                if male_voice:
                    speaker.Voice = male_voice
                    logger.info("[SpaceLord] 🎤 Using male voice: %s", male_voice.GetDescription())
                else:
                    logger.warning("[SpaceLord] ⚠️ No male voice found, using Windows default SAPI voice")
                
                # Generate the speech file
                stream = win32com.client.Dispatch("SAPI.SpFileStream")
                stream.Open(temp_file, 3)  # 3 = SSFMOpenWriteOnly | SSFMOpenCreate
                speaker.AudioOutputStream = stream
                speaker.Speak(text)
                stream.Close()
                
                # Give Windows a moment to finish writing the file
                await asyncio.sleep(0.5)
                
                if os.path.exists(temp_file):
                    # Queue for playback using the audio player
                    success = await self.tts_system.audio_player.play_audio(temp_file)
                    
                    if success:
                        logger.info(f"[SpaceLord] 🔊 Successfully queued male voice speech: {text[:50]}...")
                    else:
                        logger.error(f"[SpaceLord] ❌ Failed to queue male voice speech")
                    
                    return success
                else:
                    logger.error(f"[SpaceLord] ❌ Male voice TTS file not created")
                    return False
            else:
                logger.error(f"[SpaceLord] ❌ Windows TTS not available for male voice")
                return False
                
        except Exception as e:
            logger.error(f"[SpaceLord] Error in male voice TTS: {e}")
            return False
    
    async def _find_best_male_voice(self, speaker) -> object:
        """Find the best available male voice (installed Windows / SAPI voice packs)."""
        try:
            voices = speaker.GetVoices()
            male_voices = []

            for i in range(voices.Count):
                voice = voices.Item(i)
                voice_desc = voice.GetDescription().lower()

                if any(
                    male_name in voice_desc
                    for male_name in [
                        'david',
                        'mark',
                        'james',
                        'mike',
                        'steve',
                        'john',
                        'paul',
                        'chris',
                        'christopher',
                        'michael',
                        'robert',
                        'william',
                        'male',
                        'guy',
                        'man',
                        'boy',
                        'dude',
                    ]
                ):
                    male_voices.append(voice)
                    logger.debug("[SpaceLord] Found male voice: %s", voice.GetDescription())

            if male_voices:
                neural_voices = [v for v in male_voices if 'neural' in v.GetDescription().lower()]
                if neural_voices:
                    return neural_voices[0]
                return male_voices[0]

            logger.warning("[SpaceLord] ⚠️ No male voices found, will use default")
            return None

        except Exception as e:
            logger.error("[SpaceLord] Error finding male voice: %s", e)
            return None


class HomeyBotHost:
    """Main bot class — local/host audio routing (capturable by OBS)."""
    
    def __init__(self, config_path="config.yaml", audio_device=None):
        self._config_path = os.path.abspath(str(config_path))
        self.config = self.load_config(self._config_path)
        
        # Get audio device from config if not specified (auto / empty → Windows default output)
        if audio_device is None:
            audio_device = self.config.get('audio', {}).get('device', 'default')
        raw_dev = (audio_device or "").strip().lower()
        if raw_dev in ("auto", ""):
            audio_device = "default"
        
        logger.info(f"[HomeyBotHost] 🎵 Using audio device: {audio_device}")
        logger.info(f"[HomeyBotHost] 🎵 Config audio device: {self.config.get('audio', {}).get('device', 'NOT_FOUND')}")
        
        # Log available audio devices for debugging
        self._log_available_audio_devices()
        
        self.audio_player = DesktopAudioPlayer(audio_device)
        self.tts_system = DesktopTTS(self.audio_player)
        self.twitch_bot = TwitchBot(self.config, self.tts_system, config_path=self._config_path)
        self.twitch_task = None
    
    def _log_available_audio_devices(self):
        """Log all available audio devices for debugging."""
        try:
            import pyaudio
            p = pyaudio.PyAudio()
            device_count = p.get_device_count()
            logger.info(f"[HomeyBotHost] 🔍 Available audio devices ({device_count} total):")
            
            for i in range(device_count):
                device_info = p.get_device_info_by_index(i)
                device_name = device_info.get('name', 'Unknown')
                max_output_channels = device_info.get('maxOutputChannels', 0)
                is_default = device_info.get('index') == p.get_default_output_device_info()['index']
                default_marker = " (DEFAULT)" if is_default else ""
                
                if max_output_channels > 0:  # Only show output devices
                    logger.info(f"[HomeyBotHost]   {i}: {device_name}{default_marker}")
            
            p.terminate()
        except Exception as e:
            logger.error(f"[HomeyBotHost] ❌ Error listing audio devices: {e}")
    
    def load_config(self, config_path: str | os.PathLike[str]) -> dict[str, Any]:
        """Load configuration from YAML file."""
        try:
            with open(config_path, 'r', encoding='utf-8') as file:
                config = yaml.safe_load(file)
            if not isinstance(config, dict):
                raise ValueError("config YAML root must be a mapping (dictionary)")
            logger.info("[Config] ✅ Configuration loaded successfully")
            return config
        except Exception as e:
            logger.error(f"[Config] ❌ Error loading config: {e}")
            # Return default config
            return {
                'twitch': {
                    'bot_token': 'YOUR_TWITCH_TOKEN',
                    'client_id': 'YOUR_TWITCH_CLIENT_ID',
                    'bot_username': 'YOUR_TWITCH_BOT_USERNAME',
                    'channel': 'YOUR_TWITCH_CHANNEL'
                }
            }
    
    async def start(self):
        """Start the Twitch bot with Bluetooth audio."""
        try:
            logger.info(f"[HomeyBotHost] 🚀 Starting Homey Bot with host audio (device: {self.audio_player.audio_device})...")
            
            # Send to GUI if available
            if GUI_AVAILABLE:
                add_gui_message(f"🚀 Starting Homey Bot with host audio (device: {self.audio_player.audio_device})...", "INFO")
                add_gui_message("🎤 Using Windows TTS with female voice", "TTS")
                add_gui_message(f"🔊 Audio output: {self.audio_player.audio_device} (capturable by OBS)", "AUDIO")
            
            # Start the audio player
            logger.info(f"[HomeyBotHost] 🎵 Starting audio processor...")
            await self.audio_player.start_audio_processor()
            logger.info(f"[HomeyBotHost] ✅ Audio processor started")
            
            # Start Twitch bot with error handling
            logger.info(f"[HomeyBotHost] 🤖 Creating Twitch bot task...")
            self.twitch_task = asyncio.create_task(self._run_twitch_bot_with_retry())
            logger.info(f"[HomeyBotHost] ✅ Twitch bot task created")
            
            # No status checker needed - just focus on reading chat
            
            # Wait for the bot
            logger.info(f"[HomeyBotHost] ⏳ Waiting for Twitch bot task to complete...")
            await self.twitch_task
            logger.info(f"[HomeyBotHost] 🎯 Twitch bot task completed")
            
            # Keep the bot alive with a simple loop
            logger.info(f"[HomeyBotHost] 🔄 Starting keep-alive loop...")
            try:
                while True:
                    await asyncio.sleep(60)  # Check every minute
                    logger.debug(f"[HomeyBotHost] 💓 Bot still alive - {time.strftime('%H:%M:%S')}")
            except KeyboardInterrupt:
                logger.info(f"[HomeyBotHost] 🛑 Keep-alive loop interrupted by user")
            except Exception as e:
                logger.error(f"[HomeyBotHost] ❌ Error in keep-alive loop: {e}")
            
        except asyncio.CancelledError:
            logger.info("[HomeyBotHost] Bot startup cancelled")
        except Exception as e:
            logger.error(f"[HomeyBotHost] Error starting bot: {e}")
            import traceback
            logger.error(f"[HomeyBotHost] Traceback: {traceback.format_exc()}")
    
    async def _run_twitch_bot_with_retry(self):
        """Run Twitch bot with automatic retry on failure."""
        max_retries = 5
        retry_delay = 10  # Start with 10 seconds
        
        for attempt in range(max_retries):
            try:
                logger.info(f"[HomeyBotHost] 🚀 Starting Twitch bot (attempt {attempt + 1}/{max_retries})")
                if attempt > 0:
                    # Ensure a failed/partial login does not skip setup_hook on the next start()
                    self.twitch_bot._login_called = False
                    self.twitch_bot._setup_called = False

                # Start the Twitch bot and keep it running
                logger.info(f"[HomeyBotHost] 🎯 Starting Twitch bot.start()...")
                
                # Start the bot and keep it running
                logger.info(f"[HomeyBotHost] 🎯 Starting Twitch bot.start() - this should run indefinitely...")
                
                # The bot.start() method should run indefinitely, listening for messages
                await self.twitch_bot.start()
                
                # If we get here, the bot task completed (which shouldn't happen)
                logger.warning(f"[HomeyBotHost] ⚠️ Twitch bot task completed unexpectedly!")
                logger.warning(f"[HomeyBotHost] ⚠️ This might be normal if the bot was stopped intentionally")
                
                # Don't retry if the bot completed normally
                break
                
            except Exception as e:
                logger.error(f"[HomeyBotHost] Twitch bot failed (attempt {attempt + 1}/{max_retries}): {e}")
                if isinstance(e, TwitchHTTPException):
                    logger.error(
                        "[Twitch][debug] retry loop caught TwitchHTTPException: %s",
                        self.twitch_bot._tw_http_exc_extras(e),
                    )
                import traceback
                logger.error(f"[HomeyBotHost] Traceback: {traceback.format_exc()}")
                
                if attempt < max_retries - 1:
                    logger.info(f"[HomeyBotHost] 🔄 Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error(f"[HomeyBotHost] ❌ Twitch bot failed after {max_retries} attempts")
                    raise
        
        logger.info(f"[HomeyBotHost] 🎯 Twitch bot loop completed")
    

    
    async def stop(self):
        """Stop the bot."""
        try:
            logger.info("[HomeyBotHost] 🛑 Stopping Homey Bot...")
            
            # Stop audio processing
            await self.audio_player.stop_audio_processor()
            await self.audio_player.clear_queue()
            
            # Clean up any remaining temporary files
            await self.audio_player.cleanup_temp_files()
            
            # Force cleanup of any remaining files with multiple attempts
            await self._force_cleanup_remaining_files()
            
            lp = getattr(self.twitch_bot, "_discord_listen_proc", None)
            tp = getattr(self.twitch_bot, "_discord_tx_proc", None)
            qu = getattr(self.twitch_bot, "_discord_pcm_queue", None)
            if lp is not None or tp is not None:
                try:
                    if lp is not None and lp.is_alive():
                        lp.terminate()
                        lp.join(timeout=8)
                except Exception as e:
                    logger.warning("[HomeyBotHost] Error stopping Discord listen worker: %s", e)
                try:
                    if qu is not None:
                        qu.put(None, timeout=2)
                except Exception:
                    pass
                try:
                    if tp is not None and tp.is_alive():
                        tp.join(timeout=45)
                except Exception as e:
                    logger.warning("[HomeyBotHost] Error stopping Discord transcribe worker: %s", e)
                    try:
                        if tp is not None:
                            tp.terminate()
                            tp.join(timeout=5)
                    except Exception:
                        pass
                self.twitch_bot._discord_listen_proc = None
                self.twitch_bot._discord_tx_proc = None
                self.twitch_bot._discord_pcm_queue = None
                logger.info("[HomeyBotHost] Discord voice worker processes stopped")

            # Cancel Twitch bot
            if self.twitch_task:
                self.twitch_task.cancel()
            

            
            # Close the Twitch bot
            try:
                await self.twitch_bot.close()
            except Exception as e:
                logger.error(f"[HomeyBotHost] Error closing Twitch bot: {e}")
            
            logger.info("[HomeyBotHost] ✅ Bot stopped successfully")
            
        except Exception as e:
            logger.error(f"[HomeyBotHost] Error stopping bot: {e}")
    
    async def _force_cleanup_remaining_files(self):
        """Simple cleanup of any remaining temporary files."""
        try:
            import glob
            temp_patterns = ["temp_tts_windows_*.wav", "temp_tts_edge_*.mp3"]
            
            cleaned_count = 0
            for pattern in temp_patterns:
                for file_path in glob.glob(pattern):
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            cleaned_count += 1
                            logger.debug(f"[HomeyBotHost] 🧹 Cleaned up: {os.path.basename(file_path)}")
                    except Exception as e:
                        logger.debug(f"[HomeyBotHost] Could not clean up {os.path.basename(file_path)}: {e}")
            
            if cleaned_count > 0:
                logger.info(f"[HomeyBotHost] 🧹 Cleaned up {cleaned_count} remaining files")
                
        except Exception as e:
            logger.error(f"[HomeyBotHost] Error in force cleanup: {e}")

async def main():
    """Main function."""
    import sys
    
    # Parse command line arguments for audio device
    audio_device = None  # Will be read from config file
    
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().lower()
        _legacy_alias = "".join(map(chr, (100, 101, 115, 107, 116, 111, 112)))
        if arg in ("default", "bluetooth", "pc") or arg == _legacy_alias:
            if arg == "bluetooth":
                audio_device = "bluetooth"
            else:
                audio_device = "default"
        else:
            print(f"Usage: python {sys.argv[0]} [default|bluetooth|pc]")
            print("  default: System default output (OBS-friendly)")
            print("  bluetooth: Bluetooth output device")
            print("  pc: Same routing as default")
            print("  (no argument): Use device from config.yaml")
            print("Using audio device from config file")
    
    logger.info(f"Audio device: {audio_device if audio_device else 'from config.yaml'}")
    
    bot = HomeyBotHost(audio_device=audio_device)
    
    # Auto-start GUI if available
    if GUI_AVAILABLE:
        try:
            # Start GUI in a separate thread
            gui_thread = threading.Thread(target=start_gui, daemon=True)
            gui_thread.start()
            logger.info("[GUI] 🚀 GUI monitor started automatically")
            
            # Give GUI a moment to initialize
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"[GUI] Failed to start GUI: {e}")
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("[Main] Received interrupt signal")
    except asyncio.CancelledError:
        logger.info("[Main] Bot operation cancelled")
    except Exception as e:
        logger.error(f"[Main] Unexpected error: {e}")
        import traceback
        logger.error(f"[Main] Traceback: {traceback.format_exc()}")
        
        # Try to restart the bot automatically
        logger.info("[Main] 🔄 Attempting to restart bot in 30 seconds...")
        await asyncio.sleep(30)
        try:
            await bot.start()
        except Exception as restart_error:
            logger.error(f"[Main] ❌ Bot restart failed: {restart_error}")
    finally:
        try:
            await bot.stop()
        except Exception as e:
            logger.error(f"[Main] Error during cleanup: {e}")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    asyncio.run(main())
