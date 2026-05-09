#!/usr/bin/env python3
"""
Homey Bot GUI Monitor

A simple GUI that shows what the bot is currently saying and the last 10 messages.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import threading
import queue
import time
from datetime import datetime

class HomeyBotGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🏠 Homey Bot Monitor")
        self.root.geometry("1200x800")
        self.root.configure(bg='#2b2b2b')
        
        # Message queue for thread-safe communication
        self.message_queue = queue.Queue()
        
        # Message history (last 10)
        self.message_history = []
        self.max_history = 10
        
        # Space Lord API history
        self.should_respond_history = []
        self.generate_response_history = []
        self.max_api_history = 5
        
        self.setup_gui()
        self.start_update_loop()
    
    def setup_gui(self):
        """Setup the GUI elements."""
        # Main title
        title_label = tk.Label(
            self.root,
            text="🏠 Homey Bot - Twitch Chat Reader",
            font=("Arial", 16, "bold"),
            bg='#2b2b2b',
            fg='#ffffff'
        )
        title_label.pack(pady=10)
        
        # Create main container for left and right panels
        main_container = tk.Frame(self.root, bg='#2b2b2b')
        main_container.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Left panel (original content)
        left_panel = tk.Frame(main_container, bg='#2b2b2b')
        left_panel.pack(side="left", fill="both", expand=True, padx=5)
        
        # Right panel (Space Lord API)
        right_panel = tk.Frame(main_container, bg='#2b2b2b')
        right_panel.pack(side="right", fill="both", expand=True, padx=5)
        
        # Current activity frame
        current_frame = tk.LabelFrame(
            left_panel,
            text="🎤 Currently Speaking",
            font=("Arial", 12, "bold"),
            bg='#2b2b2b',
            fg='#ffffff',
            relief="ridge",
            bd=2
        )
        current_frame.pack(fill="x", padx=10, pady=5)
        
        # Current message display
        self.current_message_var = tk.StringVar(value="Waiting for messages...")
        self.current_message_label = tk.Label(
            current_frame,
            textvariable=self.current_message_var,
            font=("Arial", 11),
            bg='#2b2b2b',
            fg='#00ff00',
            wraplength=550,
            justify="center"
        )
        self.current_message_label.pack(pady=15, padx=20)
        
        # Status indicator
        self.status_var = tk.StringVar(value="🟡 Idle")
        self.status_label = tk.Label(
            current_frame,
            textvariable=self.status_var,
            font=("Arial", 10, "bold"),
            bg='#2b2b2b',
            fg='#ffff00'
        )
        self.status_label.pack(pady=5)
        
        # Message history frame
        history_frame = tk.LabelFrame(
            left_panel,
            text="📝 Recent Messages (Last 10)",
            font=("Arial", 12, "bold"),
            bg='#2b2b2b',
            fg='#ffffff',
            relief="ridge",
            bd=2
        )
        history_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # History text area
        self.history_text = scrolledtext.ScrolledText(
            history_frame,
            height=12,
            font=("Consolas", 9),
            bg='#1e1e1e',
            fg='#ffffff',
            insertbackground='#ffffff',
            wrap=tk.WORD
        )
        self.history_text.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Space Lord API sections (right panel)
        self.setup_space_lord_api_sections(right_panel)
        
        # Control buttons frame
        button_frame = tk.Frame(self.root, bg='#2b2b2b')
        button_frame.pack(pady=10)
        
        # Clear history button
        clear_button = tk.Button(
            button_frame,
            text="🗑️ Clear History",
            command=self.clear_history,
            bg='#ff4444',
            fg='#ffffff',
            font=("Arial", 10, "bold"),
            relief="raised",
            bd=2
        )
        clear_button.pack(side="left", padx=5)
        
        # Refresh button
        refresh_button = tk.Button(
            button_frame,
            text="🔄 Refresh",
            command=self.refresh_display,
            bg='#4444ff',
            fg='#ffffff',
            font=("Arial", 10, "bold"),
            relief="raised",
            bd=2
        )
        refresh_button.pack(side="left", padx=5)
        
        # Stats button
        stats_button = tk.Button(
            button_frame,
            text="📊 Stats",
            command=self.show_stats,
            bg='#44ff44',
            fg='#000000',
            font=("Arial", 10, "bold"),
            relief="raised",
            bd=2
        )
        stats_button.pack(side="left", padx=5)
        
        # Footer
        footer_label = tk.Label(
            self.root,
            text="GUI updates automatically every 100ms",
            font=("Arial", 8),
            bg='#2b2b2b',
            fg='#888888'
        )
        footer_label.pack(pady=5)
    
    def setup_space_lord_api_sections(self, parent):
        """Setup Space Lord API monitoring sections."""
        # Should Respond API frame
        should_respond_frame = tk.LabelFrame(
            parent,
            text="🚀 Space Lord - Should Respond API",
            font=("Arial", 11, "bold"),
            bg='#2b2b2b',
            fg='#ff8800',
            relief="ridge",
            bd=2
        )
        should_respond_frame.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Should Respond API text area
        self.should_respond_text = scrolledtext.ScrolledText(
            should_respond_frame,
            height=8,
            font=("Consolas", 8),
            bg='#1e1e1e',
            fg='#ff8800',
            insertbackground='#ff8800',
            wrap=tk.WORD
        )
        self.should_respond_text.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Generate Response API frame
        generate_response_frame = tk.LabelFrame(
            parent,
            text="🤖 Space Lord - Generate Response API",
            font=("Arial", 11, "bold"),
            bg='#2b2b2b',
            fg='#00ff88',
            relief="ridge",
            bd=2
        )
        generate_response_frame.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Generate Response API text area
        self.generate_response_text = scrolledtext.ScrolledText(
            generate_response_frame,
            height=8,
            font=("Consolas", 8),
            bg='#1e1e1e',
            fg='#00ff88',
            insertbackground='#00ff88',
            wrap=tk.WORD
        )
        self.generate_response_text.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Voice Listener frame
        voice_listener_frame = tk.LabelFrame(
            parent,
            text="🎤 Voice Listener - Wake Word Detection",
            font=("Arial", 11, "bold"),
            bg='#2b2b2b',
            fg='#ff44aa',
            relief="ridge",
            bd=2
        )
        voice_listener_frame.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Voice Listener text area
        self.voice_listener_text = scrolledtext.ScrolledText(
            voice_listener_frame,
            height=6,
            font=("Consolas", 8),
            bg='#1e1e1e',
            fg='#ff44aa',
            insertbackground='#ff44aa',
            wrap=tk.WORD
        )
        self.voice_listener_text.pack(fill="both", expand=True, padx=5, pady=5)
    
    def add_message(self, message: str, message_type: str = "TTS"):
        """Add a message to the history and update display."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        # Add to history
        history_entry = {
            'timestamp': timestamp,
            'message': message,
            'type': message_type
        }
        
        self.message_history.append(history_entry)
        
        # Keep only last 10 messages
        if len(self.message_history) > self.max_history:
            self.message_history.pop(0)
        
        # Update current message if it's a TTS message
        if message_type == "TTS":
            self.current_message_var.set(message)
            self.status_var.set("🟢 Speaking")
        
        # Update history display
        self.update_history_display()
        
        # Handle Space Lord API messages
        if message_type == "SHOULD_RESPOND_API":
            self.add_should_respond_api_message(message)
        elif message_type == "GENERATE_RESPONSE_API":
            self.add_generate_response_api_message(message)
        elif message_type == "VOICE_LISTENER":
            self.add_voice_listener_message(message)
    
    def add_should_respond_api_message(self, message: str):
        """Add a should respond API message to the display."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.should_respond_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.should_respond_text.see(tk.END)
        
        # Keep only last few entries
        lines = self.should_respond_text.get(1.0, tk.END).split('\n')
        if len(lines) > 20:  # Keep last 20 lines
            self.should_respond_text.delete(1.0, tk.END)
            self.should_respond_text.insert(tk.END, '\n'.join(lines[-20:]) + '\n')
    
    def add_generate_response_api_message(self, message: str):
        """Add a generate response API message to the display."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.generate_response_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.generate_response_text.see(tk.END)
        
        # Keep only last few entries
        lines = self.generate_response_text.get(1.0, tk.END).split('\n')
        if len(lines) > 20:  # Keep last 20 lines
            self.generate_response_text.delete(1.0, tk.END)
            self.generate_response_text.insert(tk.END, '\n'.join(lines[-20:]) + '\n')
    
    def add_voice_listener_message(self, message: str):
        """Add a voice listener message to the display."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.voice_listener_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.voice_listener_text.see(tk.END)
        
        # Keep only last few entries
        lines = self.voice_listener_text.get(1.0, tk.END).split('\n')
        if len(lines) > 15:  # Keep last 15 lines for voice listener
            self.voice_listener_text.delete(1.0, tk.END)
            self.voice_listener_text.insert(tk.END, '\n'.join(lines[-15:]) + '\n')
    
    def update_history_display(self):
        """Update the history text area."""
        self.history_text.delete(1.0, tk.END)
        
        for entry in reversed(self.message_history):  # Show newest first
            timestamp = entry['timestamp']
            message = entry['message']
            msg_type = entry['type']
            
            # Color coding based on message type
            if msg_type == "TTS":
                prefix = "🎤"
                color_tag = "tts"
            elif msg_type == "ERROR":
                prefix = "❌"
                color_tag = "error"
            elif msg_type == "INFO":
                prefix = "ℹ️"
                color_tag = "info"
            else:
                prefix = "📝"
                color_tag = "normal"
            
            line = f"[{timestamp}] {prefix} {message}\n"
            self.history_text.insert(tk.END, line)
            
            # Apply color tags
            text_content = self.history_text.get(1.0, tk.END)
            lines = text_content.split(chr(10))
            line_count = len(lines) - 1
            start = f"{line_count}.0"
            end = f"{line_count}.end"
            
            if msg_type == "TTS":
                self.history_text.tag_add("tts", start, end)
                self.history_text.tag_config("tts", foreground="#00ff00")
            elif msg_type == "ERROR":
                self.history_text.tag_add("error", start, end)
                self.history_text.tag_config("error", foreground="#ff4444")
            elif msg_type == "INFO":
                self.history_text.tag_add("info", start, end)
                self.history_text.tag_config("info", foreground="#4444ff")
    
    def clear_history(self):
        """Clear the message history."""
        self.message_history.clear()
        self.current_message_var.set("History cleared")
        self.status_var.set("🟡 Idle")
        self.update_history_display()
    
    def refresh_display(self):
        """Refresh the display."""
        self.update_history_display()
        self.root.update()
    
    def show_stats(self):
        """Show statistics about the bot."""
        total_messages = len(self.message_history)
        tts_messages = len([m for m in self.message_history if m['type'] == 'TTS'])
        error_messages = len([m for m in self.message_history if m['type'] == 'ERROR'])
        
        stats_text = f"""📊 Bot Statistics:

Total Messages: {total_messages}
TTS Messages: {tts_messages}
Error Messages: {error_messages}
History Size: {len(self.message_history)}/{self.max_history}

Last Updated: {datetime.now().strftime("%H:%M:%S")}"""
        
        # Create stats window
        stats_window = tk.Toplevel(self.root)
        stats_window.title("📊 Bot Statistics")
        stats_window.geometry("300x250")
        stats_window.configure(bg='#2b2b2b')
        
        stats_label = tk.Label(
            stats_window,
            text=stats_text,
            font=("Consolas", 10),
            bg='#2b2b2b',
            fg='#ffffff',
            justify="left"
        )
        stats_label.pack(pady=20, padx=20)
        
        close_button = tk.Button(
            stats_window,
            text="Close",
            command=stats_window.destroy,
            bg='#666666',
            fg='#ffffff'
        )
        close_button.pack(pady=10)
    
    def start_update_loop(self):
        """Start the update loop for the GUI."""
        def update_loop():
            while True:
                try:
                    # Check for new messages
                    try:
                        message_data = self.message_queue.get_nowait()
                        self.add_message(message_data['message'], message_data['type'])
                    except queue.Empty:
                        pass
                    
                    # Update status if no recent TTS activity
                    if self.message_history:
                        last_tts = None
                        for entry in reversed(self.message_history):
                            if entry['type'] == 'TTS':
                                last_tts = entry
                                break
                        
                        if last_tts:
                            # Check if last TTS was more than 5 seconds ago
                            last_time = datetime.strptime(last_tts['timestamp'], "%H:%M:%S")
                            current_time = datetime.now().strftime("%H:%M:%S")
                            current_time = datetime.strptime(current_time, "%H:%M:%S")
                            
                            # Simple time difference (this is approximate)
                            if (current_time.second - last_time.second) > 5:
                                self.status_var.set("🟡 Idle")
                    
                    time.sleep(0.1)  # Update every 100ms
                    
                except Exception as e:
                    print(f"GUI update error: {e}")
                    time.sleep(1)
        
        # Start update thread
        update_thread = threading.Thread(target=update_loop, daemon=True)
        update_thread.start()
    
    def run(self):
        """Run the GUI."""
        self.root.mainloop()

# Global instance for external access
gui_instance = None

def start_gui():
    """Start the GUI monitor."""
    global gui_instance
    gui_instance = HomeyBotGUI()
    gui_instance.run()

def add_gui_message(message: str, message_type: str = "TTS"):
    """Add a message to the GUI from external code."""
    if gui_instance and gui_instance.message_queue:
        gui_instance.message_queue.put({
            'message': message,
            'type': message_type
        })

if __name__ == "__main__":
    # Test the GUI
    start_gui()
