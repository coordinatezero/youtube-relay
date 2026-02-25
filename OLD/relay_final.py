import subprocess
import socket
import time
import threading
import sys
import json
import os

# ================= CONFIGURATION =================
# 1. INTERNAL CONFIG
CONFIG_FILE = "/etc/youtube-relay/config.json"
INPUT_RTMP = "rtmp://localhost:1935/live/berkshire" # Local NGINX source

# 2. INTERNAL LOCALHOST SETTINGS (Do not change)
RELAY_HOST = "127.0.0.1"
RELAY_PORT = 10000
WIDTH = 1920
HEIGHT = 1080
FPS = 30
# =================================================

def load_config():
    """Loads stream key and metadata from JSON config."""
    if not os.path.exists(CONFIG_FILE):
        print(f"[!] FATAL: Config file not found at {CONFIG_FILE}")
        sys.exit(1)
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            
        required = ["stream_key"]
        for key in required:
            if key not in config:
                print(f"[!] FATAL: Missing '{key}' in {CONFIG_FILE}")
                sys.exit(1)
                
        return config
    except json.JSONDecodeError as e:
        print(f"[!] FATAL: Invalid JSON in config file: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[!] FATAL: Could not read config: {e}")
        sys.exit(1)

# Load Config Globally
APP_CONFIG = load_config()
YOUTUBE_RTMP = f"rtmp://a.rtmp.youtube.com/live2/{APP_CONFIG['stream_key']}"

def log_subprocess(pipe, prefix):
    """Logs FFmpeg errors to console to help debugging."""
    try:
        for line in iter(pipe.readline, b''):
            msg = line.decode().strip()
            # Ignore standard progress indicators to keep log clean
            if "frame=" not in msg and "speed=" not in msg:
                print(f"[{prefix}] {msg}")
    except: pass

def get_black_generator():
    """
    Generates Black Frames.
    CRITICAL: Outputs MPEG-TS so we can cut the stream mid-broadcast.
    """
    cmd = [
        'ffmpeg', '-re', '-y', '-hide_banner', '-loglevel', 'warning',
        '-f', 'lavfi', '-i', f'color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        
        # Encoding to match the "Live" stream format
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(FPS*2),
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def get_live_generator():
    """
    Reads NGINX Stream.
    """
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
        '-rw_timeout', '5000000',   # 5s Timeout if source dies
        '-i', INPUT_RTMP,
        
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(FPS*2),
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def start_sender_process():
    """
    Connects to our Python TCP Server -> Sends to YouTube.
    Uses +genpts to fix timestamps when switching.
    """
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
        
        # Input: Read MPEG-TS from local TCP
        '-f', 'mpegts',
        '-fflags', '+genpts+igndts', # CRITICAL: Regenerate timestamps
        '-i', f'tcp://{RELAY_HOST}:{RELAY_PORT}?listen', # Listen mode
        
        # Output: Copy codec (since we encoded in generators) or Re-encode
        '-c', 'copy', 
        
        '-f', 'flv', YOUTUBE_RTMP
    ]
    return subprocess.Popen(cmd, stderr=subprocess.PIPE)

def run_server():
    print(f"[*] TCP Relay Server Started on port {RELAY_PORT}")
    print(f"[*] Loaded Config: {APP_CONFIG.get('broadcast_title', 'Unknown Title')}")
    print(f"[*] Target: YouTube ({APP_CONFIG.get('privacy', 'public')})")
    
    # 1. Start the Sender (It waits for a connection)
    print("[*] Launching YouTube Sender...")
    sender = start_sender_process()
    
    # Monitor Sender logs
    t = threading.Thread(target=log_subprocess, args=(sender.stderr, "SENDER"))
    t.daemon = True
    t.start()
    
    # Give Sender a moment to open the port
    time.sleep(1)

    # 2. Connect to the Sender
    try:
        conn = socket.create_connection((RELAY_HOST, RELAY_PORT))
        print(f"[+] Connected to Sender Pipe!")
    except ConnectionRefusedError:
        print("[!] Failed to connect to Sender. Is FFmpeg installed?")
        return

    current_source = None
    source_type = "NONE"
    
    try:
        while True:
            # Monitor Sender Health
            if sender.poll() is not None:
                print("[!] Sender died! Restarting...")
                conn.close()
                sender = start_sender_process()
                time.sleep(1)
                conn = socket.create_connection((RELAY_HOST, RELAY_PORT))

            # --- SOURCE SWITCHING ---
            if source_type != "LIVE":
                # Try to peek at NGINX
                live_test = get_live_generator()
                time.sleep(0.5) # Allow buffer fill
                
                if live_test.poll() is None:
                    print("\n[+] DETECTED LIVE STREAM! Switching...")
                    if current_source: current_source.kill()
                    current_source = live_test
                    source_type = "LIVE"
                else:
                    # Live failed, ensure Black is running
                    live_test.kill()
                    if source_type != "BLACK":
                        print("\n[-] No Input. Switching to BLACK frames.")
                        if current_source: current_source.kill()
                        current_source = get_black_generator()
                        source_type = "BLACK"

            # --- DATA PUMP ---
            try:
                # Read 64kb chunks
                data = current_source.stdout.read(65536)
                
                if not data:
                    print(f"\n[!] {source_type} EOF.")
                    source_type = "NONE"
                    continue
                
                conn.sendall(data)

            except (BrokenPipeError, ConnectionResetError):
                print("[!] Connection lost. Retrying loop...")
                time.sleep(1)

    except KeyboardInterrupt:
        print("\n[*] Stopping...")
    finally:
        if current_source: current_source.kill()
        sender.kill()
        conn.close()

if __name__ == "__main__":
    run_server()
