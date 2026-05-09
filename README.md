# Homey Bot 2.0

Twitch chat → TTS (Windows / Edge voices), optional Discord persona for Space Lord, local GUI monitor.

## Quick start

1. Install **Python 3.11**.
2. Create a venv and install deps:
   ```powershell
   python -m venv venv
   .\venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. **`cp config.example.yaml config.yaml`** (or copy manually). Fill in Twitch, Discord, and OpenAI; never commit `config.yaml`.
4. Run:
   ```powershell
   python homey_bot_space_lord.py
   ```
   On Windows you can use `run_bot.bat` (adjust the path inside if your folder is named `venv` instead of `venviron`).

## Repo layout

- `homey_bot_space_lord.py` — main entry
- `config.example.yaml` — template; secrets go in ignored `config.yaml`
- `run_bot.bat` — launcher (Windows)
- `WORKING_CONFIGURATION_BENCHMARK.md` — notes and troubleshooting

## Twitch (TwitchIO 3)

Uses EventSub user token + `channel.chat.message`. Bot needs **`user:read:chat`** and moderator/bot authorization in the channel; put **`refresh_token`** in `config.yaml` beside `bot_token` when TwitchIO needs to refresh tokens.
