# Homey Bot Working Configuration - Benchmark
**Date**: October 1, 2025 (9:25 PM)
**Status**: ✅ FULLY WORKING

## Current Working State

### ✅ What's Working:
- **Twitch Chat Reading**: Bot receives and reads all Twitch chat messages
- **Text-to-Speech**: Windows TTS (Microsoft Zira - female voice for chat, Microsoft David - male voice for Space Lord)
- **Audio Output**: Headphones (B350-XT II v1.21) - Bluetooth headphones
- **Space Lord AI**: Responding to chat messages with personality
- **GUI Monitor**: Displaying all bot activity
- **Message Format**: "username says: message" (as preferred)

### Configuration

Do not commit secrets. Copy `config.example.yaml` → `config.yaml` locally and add Twitch, Discord, and OpenAI credentials. Omit `oauth:` prefix handling is done in code if present.

### Key Technical Fixes Applied:

#### 1. Windows COM Threading (CRITICAL FIX)
**Problem**: Windows TTS was blocking the async event loop
**Solution**: Run Windows TTS in thread executor with proper COM initialization

```python
def _generate_windows_tts_sync(self, text: str, temp_file: str):
    import pythoncom
    try:
        # Initialize COM for this thread
        pythoncom.CoInitialize()
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            # ... TTS generation code ...
        finally:
            pythoncom.CoUninitialize()
```

**File**: homey_bot_space_lord.py, lines 608-646

#### 2. Twitch OAuth Token Update
**Problem**: Old OAuth token had expired (messages not received)
**Solution**: Generated new token from Twitch Developer Console
- New access token
- New client_id and client_secret from dev.twitch.tv/console/apps

#### 3. Audio Device Configuration
**Problem**: Bluetooth headphones incompatible with PyAudio streaming
**Solution**: Tested multiple devices, found working configuration
- Working: "Headphones (B350-XT II v1.21)" (matched device name)
- Alternative for streaming: "Voicemeeter Input" (requires Voicemeeter routing)
- Fallback: "Speaker (Realtek(R) Audio)" (desktop speakers)

#### 4. Unicode Encoding Fix
**Problem**: Print statements with emojis causing UnicodeEncodeError
**Solution**: Changed print() to logger.info() for emoji handling
**File**: homey_bot_space_lord.py, line 2210

### How to Start the Bot:

```powershell
cd path\to\homey_bot_2_0
.\venv\Scripts\activate
pip install -r requirements.txt
python homey_bot_space_lord.py
```
(Create a virtual environment named `venv` or `venviron`; see `run_bot.bat` on Windows.)

### Audio Device Options:

1. **Bluetooth Headphones** (Current):
   - Device: `"Headphones (B350-XT II v1.21)"`
   - For: Personal listening
   
2. **Desktop Speakers**:
   - Device: `"Speaker (Realtek(R) Audio)"`
   - For: Testing, casual use
   
3. **Voicemeeter Input**:
   - Device: `"Voicemeeter Input"`
   - For: OBS streaming (requires Voicemeeter routing to hear)

### Troubleshooting:

#### If bot doesn't receive messages:
1. Check Twitch OAuth token hasn't expired
2. Verify bot is listed in channel chat user list
3. Check twitch_messages.log for logged messages

#### If audio doesn't play:
1. Verify audio device is connected and recognized by Windows
2. Check logs for "Playing audio on device: [device name]"
3. Try desktop speakers as fallback: `"Speaker (Realtek(R) Audio)"`

#### If Bluetooth doesn't work:
- Bluetooth headphones must be in "Headphones" mode (A2DP), not "Headset" mode
- Headset mode (Hands-Free) causes "[Errno -9999] Unanticipated host error"
- Reconnect Bluetooth device or use desktop speakers

### Dependencies (requirements.txt):
- Python 3.11
- twitchio==3.1.0
- discord.py==2.6.3
- pywin32 (for Windows TTS)
- pyaudio (for audio playback)
- pygame (fallback audio)
- openai (for Space Lord AI)
- pyyaml (for config)

### Test Commands:

**Test TTS only**:
```python
from homey_bot_space_lord import DesktopAudioPlayer, DesktopTTS
# Create and test TTS system
```

**Monitor messages**:
```powershell
Get-Content twitch_messages.log -Tail 10 -Wait
```

## Summary

All core functionality is working:
- ✅ Twitch connection established
- ✅ Messages received and logged
- ✅ TTS generation (female voice for chat)
- ✅ Audio playback through Bluetooth
- ✅ Space Lord AI responses (male voice)
- ✅ GUI monitoring active

**This configuration is stable and production-ready.**

