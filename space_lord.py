"""
Space Lord - An AI-powered voice handler for responding to Twitch chat.

This module implements a voice handler that:
1. Converts text to speech using Windows TTS
2. Generates AI responses using OpenAI
3. Outputs audio to desktop speakers (same as main bot)
4. Handles conversation history and context
5. Integrates with existing TTS system

Dependencies:
- pywin32: For Windows TTS conversion
- openai: For AI response generation
- loguru: For advanced logging
"""

from loguru import logger
from typing import Optional, Dict, Any
import os
import asyncio
import win32com.client
from datetime import datetime
import time
from openai import OpenAI
import httpx
import discord
from discord.ext import commands as discord_commands

# Try to import GUI functions
try:
    from gui_monitor import add_gui_message
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False
    def add_gui_message(message: str, message_type: str = "TTS"):
        pass

class SpaceLord:
    """
    Space Lord - An AI-powered voice handler for responding to Twitch chat.
    
    This class handles:
    1. AI response generation using OpenAI
    2. Text-to-speech conversion using Windows TTS
    3. Audio output to desktop speakers (same as main bot)
    4. Conversation history and context management
    5. Integration with existing TTS system
    
    Attributes:
        config (dict): Configuration dictionary
        tts_system: Reference to the main bot's TTS system
        is_speaking (bool): Whether Space Lord is currently speaking
        windows_voice (str): Windows TTS voice name
        speech_rate (int): Speech rate for text-to-speech
        openai_client: OpenAI client for AI responses
        conversation_history (list): History of conversations
        chat_history (list): History of chat messages
    """
    
    def __init__(self, config: dict, tts_system=None):
        """
        Initialize Space Lord with configuration.
        
        Args:
            config (dict): Configuration dictionary containing:
                - voices.space_lord.name: Windows TTS voice name
                - voices.space_lord.rate: Speech rate
                - openai.api_key: OpenAI API key
            tts_system: Reference to the main bot's TTS system
        """
        self.config = config
        self.tts_system = tts_system
        self.is_speaking = False
        
        # Get voice settings from config
        voice_config = config['voices']['space_lord']
        self.windows_voice = voice_config['name']
        self.speech_rate = voice_config['rate']
        
        # Initialize OpenAI API key
        openai_config = config.get('openai', {})
        self.openai_api_key = openai_config.get('api_key')
        # Initialize OpenAI client (new API style)
        self.openai_client = OpenAI(api_key=self.openai_api_key)
        
        # Initialize conversation history
        self.conversation_history = []
        self.max_history_length = 10
        
        # Initialize response cooldown
        self.last_response_time = None
        self.response_cooldown = 5  # seconds
        
        # Initialize chat history
        self.chat_history = []
        self.max_chat_history = 10
        
        logger.debug(f"[Space Lord] Initialized with voice: {self.windows_voice}, rate: {self.speech_rate}")
        
        # Initialize Discord persona
        self.discord_persona = None
        self.discord_persona_loaded = False
    
    async def _fetch_persona_from_discord(self):
        """Fetch Space Lord's persona from Discord channel."""
        try:
            if not self.config.get('discord', {}).get('bot_token'):
                logger.warning("[Space Lord] ⚠️ No Discord bot token configured, using default persona")
                return None
            
            # Create Discord client
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)
            
            persona_content = []
            
            @client.event
            async def on_ready():
                try:
                    logger.info(f"[Space Lord] 🔗 Connected to Discord as {client.user}")
                    
                    # Get the persona channel
                    channel_id = self.config['discord']['persona_channel_id']
                    channel = client.get_channel(channel_id)
                    
                    if not channel:
                        logger.error(f"[Space Lord] ❌ Could not find Discord channel {channel_id}")
                        return
                    
                    logger.info(f"[Space Lord] 📖 Fetching persona from Discord channel: {channel.name}")
                    
                    # Fetch recent messages from the persona channel
                    async for message in channel.history(limit=50):
                        if message.content.strip():
                            persona_content.append(message.content)
                    
                    # Reverse to get chronological order
                    persona_content.reverse()
                    
                    logger.info(f"[Space Lord] ✅ Fetched {len(persona_content)} persona messages from Discord")
                    
                except Exception as e:
                    logger.error(f"[Space Lord] ❌ Error fetching persona from Discord: {e}")
                finally:
                    await client.close()
            
            # Run the Discord client
            await client.start(self.config['discord']['bot_token'])
            
            if persona_content:
                # Combine all persona messages
                combined_persona = "\n\n".join(persona_content)
                logger.info(f"[Space Lord] 📝 Combined persona from Discord: {len(combined_persona)} characters")
                return combined_persona
            else:
                logger.warning("[Space Lord] ⚠️ No persona content found in Discord channel")
                return None
                
        except Exception as e:
            logger.error(f"[Space Lord] ❌ Error in Discord persona fetch: {e}")
            return None
    
    async def initialize_discord_persona(self):
        """Initialize Space Lord's persona from Discord channel."""
        try:
            logger.info("[Space Lord] 🔄 Initializing persona from Discord...")
            discord_persona = await self._fetch_persona_from_discord()
            
            if discord_persona:
                self.discord_persona = discord_persona
                self.discord_persona_loaded = True
                logger.info("[Space Lord] ✅ Persona loaded from Discord successfully")
            else:
                logger.warning("[Space Lord] ⚠️ Could not load persona from Discord, using default persona")
                
        except Exception as e:
            logger.error(f"[Space Lord] ❌ Error initializing Discord persona: {e}")
            logger.info("[Space Lord] ℹ️ Using default persona as fallback")

    def add_to_chat_history(self, username: str, message: str, response: str = None):
        """
        Add a message to the chat history.
        
        Args:
            username (str): The username of the message sender
            message (str): The message content
            response (str, optional): Space Lord's response to the message
        """
        # Add user message
        self.chat_history.append({
            'username': username,
            'message': message,
            'response': response
        })
        
        # Trim history if needed
        if len(self.chat_history) > self.max_chat_history:
            self.chat_history = self.chat_history[-self.max_chat_history:]
            
        logger.debug(f"[Space Lord] Added to chat history: {username}: {message}" + (f" -> {response}" if response else ""))
        logger.debug(f"[Space Lord] Current chat history size: {len(self.chat_history)}")

    async def should_respond(self, username: str, message: str) -> bool:
        """
        Determine if Space Lord should respond to a message.
        
        This method:
        1. Checks response cooldown
        2. Uses OpenAI to analyze the message context
        3. Makes a decision based on the analysis
        
        Args:
            username (str): The username of the message sender
            message (str): The message to check
            
        Returns:
            bool: True if Space Lord should respond, False otherwise
        """
        try:
            logger.debug(f"[Space Lord] Checking if Space Lord should respond to: {message}")
            
            # Add message to chat history
            self.add_to_chat_history(username, message)
            
            # Check cooldown
            if self.last_response_time:
                time_since_last = (datetime.now() - self.last_response_time).total_seconds()
                if time_since_last < self.response_cooldown:
                    logger.debug(f"[Space Lord] On cooldown, {self.response_cooldown - time_since_last:.1f} seconds remaining")
                    return False
            
            # Format chat history for context
            chat_context = "\n".join([
                f"{msg['username']}: {msg['message']}" + (f"\nSpace Lord: {msg['response']}" if msg['response'] else "")
                for msg in reversed(self.chat_history)  # Show oldest messages first
            ])
            
            # Create the prompt
            prompt = f"""Decide whether Space Lord should respond to the following chat message.

IMPORTANT: Space Lord should respond "yes" if ANY of these conditions are met:
1. The message directly addresses Space Lord (e.g., "Hey Space Lord", "Space Lord, who is...")
2. The message mentions Space Lord by name
3. The message asks about space, cosmic power, or Space Lord's domain
4. The message is part of an ongoing conversation that Space Lord is involved in
5. The message challenges Space Lord's authority or power
6. The message is clearly intended for Space Lord based on chat context

Recent chat messages:
{chat_context}

The most recent message is from {username}:
"{message}"

Consider:
- Is this message part of an ongoing conversation?
- Is it clearly directed at Space Lord?
- Would Space Lord have relevant information to share?

Respond with one word: "yes" or "no"

Answer:"""
            
            # Prepare API request with Discord persona
            system_message = self.discord_persona if self.discord_persona_loaded else "You are Space Lord, an intergalactic overlord and stream moderator. You should respond to messages that are directed at you or that you have relevant information about."
            
            api_messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ]
            
            # Log the complete API request
            logger.info("=" * 80)
            logger.info("[Space Lord] 🚀 SHOULD_RESPOND API REQUEST:")
            logger.info("=" * 80)
            logger.info(f"Model: gpt-3.5-turbo")
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
            if GUI_AVAILABLE:
                gui_message = f"🚀 SHOULD_RESPOND API REQUEST:\nModel: gpt-3.5-turbo\nTemperature: 0.7\nMax Tokens: 100\n\n📤 MESSAGES SENT TO API:\n"
                for i, msg in enumerate(api_messages):
                    gui_message += f"Message {i+1} ({msg['role']}):\n{msg['content'][:200]}...\n\n"
                add_gui_message(gui_message, "SHOULD_RESPOND_API")
            
            # Get response from OpenAI
            response = self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
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
            if GUI_AVAILABLE:
                gui_message = f"📥 SHOULD_RESPOND API RESPONSE:\nRaw Response: {response_text}\nShould Respond: {should_respond}\nUsage: {response.usage}"
                add_gui_message(gui_message, "SHOULD_RESPOND_API")
            
            # Update last response time if we're going to respond
            if should_respond:
                self.last_response_time = datetime.now()
            
            return should_respond
            
        except Exception as e:
            logger.error(f"[Space Lord] Error in should_respond: {str(e)}")
            return False

    async def generate_response(self, message: str) -> Optional[str]:
        """
        Generate a response to a message using OpenAI.
        """
        try:
            logger.debug("[Space Lord] Sending prompt to OpenAI")

            # Add message to conversation history
            self.conversation_history.append({"role": "user", "content": message})
            # Trim history if needed
            if len(self.conversation_history) > self.max_history_length:
                self.conversation_history = self.conversation_history[-self.max_history_length:]
            
            # Format chat history for context
            chat_context = "\n".join([
                f"{msg['username']}: {msg['message']}" + (f"\nSpace Lord: {msg['response']}" if msg['response'] else "")
                for msg in reversed(self.chat_history)  # Show oldest messages first
            ])
            
            # Create the system message using Discord persona and context
            default_system = f"""You are Space Lord, an intergalactic overlord and stream moderator. Use the following context to generate your response:

Recent Chat History:
{chat_context}

Current Message to Respond to:
{message}

Instructions:
1. Maintain your Space Lord character and personality
2. Consider the chat history for context
3. Keep responses concise and witty
4. Stay in character as Space Lord
5. Be helpful but maintain your cosmic authority
6. Respond as if you're a powerful space overlord with wisdom and humor"""

            # Use Discord persona if available, otherwise use default
            system_content = self.discord_persona if self.discord_persona_loaded else default_system
            
            system_message = {
                "role": "system",
                "content": system_content
            }
            
            # Combine system message with conversation history
            api_messages = [system_message] + self.conversation_history
            
            # Log the complete API request
            logger.info("=" * 80)
            logger.info("[Space Lord] 🚀 GENERATE_RESPONSE API REQUEST:")
            logger.info("=" * 80)
            logger.info(f"Model: gpt-3.5-turbo")
            logger.info(f"Temperature: 0.7")
            logger.info(f"Max Tokens: 150")
            logger.info("")
            logger.info("📤 MESSAGES SENT TO API:")
            for i, msg in enumerate(api_messages):
                logger.info(f"Message {i+1} ({msg['role']}):")
                logger.info(f"Content: {msg['content']}")
                logger.info("")
            logger.info("=" * 80)
            
            # Send to GUI
            if GUI_AVAILABLE:
                gui_message = f"🤖 GENERATE_RESPONSE API REQUEST:\nModel: gpt-3.5-turbo\nTemperature: 0.7\nMax Tokens: 150\n\n📤 MESSAGES SENT TO API:\n"
                for i, msg in enumerate(api_messages):
                    gui_message += f"Message {i+1} ({msg['role']}):\n{msg['content'][:200]}...\n\n"
                add_gui_message(gui_message, "GENERATE_RESPONSE_API")
            
            # Get response from OpenAI
            response = self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=api_messages,
                temperature=0.7,
                max_tokens=150
            )
            
            # Extract the response
            response_text = response.choices[0].message.content.strip()
            
            # Log the API response
            logger.info("=" * 80)
            logger.info("[Space Lord] 📥 GENERATE_RESPONSE API RESPONSE:")
            logger.info("=" * 80)
            logger.info(f"Generated Response: {response_text}")
            logger.info(f"Usage: {response.usage}")
            logger.info("=" * 80)
            
            # Send to GUI
            if GUI_AVAILABLE:
                gui_message = f"📥 GENERATE_RESPONSE API RESPONSE:\nGenerated Response: {response_text}\nUsage: {response.usage}"
                add_gui_message(gui_message, "GENERATE_RESPONSE_API")
            
            # Add response to conversation history
            self.conversation_history.append({"role": "assistant", "content": response_text})
            
            # Update chat history with the response
            if self.chat_history:
                self.chat_history[-1]['response'] = response_text
                
            logger.info(f"[Space Lord] ✅ Final response: {response_text}")
            return response_text
            
        except Exception as e:
            logger.error(f"[Space Lord] Error generating response: {str(e)}")
            return None

    async def speak(self, text: str) -> bool:
        """
        Speak text using the existing TTS system.
        
        This method:
        1. Uses the main bot's TTS system to speak the text
        2. Outputs audio to the same destination as the main bot
        
        Args:
            text (str): The text to speak
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            logger.debug(f"[Space Lord] Speaking: {text}")
            
            if not self.tts_system:
                logger.error("[Space Lord] TTS system not available")
                return False
            
            # Use the existing TTS system to speak
            success = await self.tts_system.speak(text)
            
            if success:
                logger.debug(f"[Space Lord] Successfully queued speech: {text}")
            else:
                logger.error(f"[Space Lord] Failed to queue speech: {text}")
                
            return success
                
        except Exception as e:
            logger.error(f"[Space Lord] Error speaking: {str(e)}")
            return False

    def stop(self):
        """
        Stop the voice handler and clean up resources.
        
        This method:
        1. Stops the voice handler
        2. Cleans up any remaining resources
        """
        logger.debug("Stopping Space Lord voice handler")
        # Note: Discord connection cleanup is handled by the main bot
        logger.debug("Space Lord voice handler stopped")
