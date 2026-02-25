import subprocess
import socket
import time
import threading
import sys
import json
import os
import logging
import pickle

# --- Google API Imports ---
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ================= CONFIGURATION =================
# 1. FILE PATHS
CONFIG_FILE = "/etc/youtube-relay/config.json"
TOKEN_PATH = '/etc/youtube-relay/token.pickle'
CLIENT_SECRET_PATH = '/etc/youtube-relay/client_secret.json'

# 2. RTMP SETTINGS
INPUT_RTMP = "rtmp://localhost:1935/live/berkshire" # Local NGINX source

# 3. INTERNAL LOCALHOST SETTINGS (Do not change)
RELAY_HOST = "127.0.0.1"
RELAY_PORT = 10000
WIDTH = 1920
HEIGHT = 1080
FPS = 30

# Setup Logging to Console
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s'
)
# =================================================

class YouTubeAPIHelper:
    """
    Helper class to interact with YouTube API for stream settings.
    (Integrated from your original script)
    """

    SCOPES = ['https://www.googleapis.com/auth/youtube']

    def __init__(self):
        self.service = None
        self.authenticate()

    def authenticate(self):
        """Authenticate with YouTube API using existing OAuth2 token"""
        creds = None

        if not os.path.exists(TOKEN_PATH):
            logging.error(f"token.pickle not found at {TOKEN_PATH}")
            return

        # Load existing credentials
        try:
            with open(TOKEN_PATH, 'rb') as token:
                creds = pickle.load(token)
        except Exception as e:
            logging.error(f"Failed to load token: {e}")
            return

        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            logging.info("Refreshing YouTube API credentials...")
            try:
                creds.refresh(Request())
                # Save refreshed credentials
                with open(TOKEN_PATH, 'wb') as token:
                    pickle.dump(creds, token)
            except Exception as e:
                logging.error(f"Failed to refresh token: {e}")
                return

        if not creds or not creds.valid:
            logging.error("YouTube credentials invalid.")
            return

        try:
            self.service = build('youtube', 'v3', credentials=creds)
            # Verify auth by grabbing channel info
            resp = self.service.channels().list(part='snippet', mine=True).execute()
            items = resp.get('items', [])
            channel_title = items[0].get('snippet', {}).get('title') if items else "Unknown"
            logging.info(f"✓ Authenticated as channel: {channel_title}")
        except Exception as e:
            logging.error(f"API Connection failed: {e}")
            self.service = None

    def get_active_broadcast(self):
        """Get the currently active or upcoming live broadcast"""
        if not self.service: return None
        try:
            request = self.service.liveBroadcasts().list(
                part='id,snippet,status',
                broadcastType='all',
                mine=True,
                maxResults=10
            )
            response = request.execute()
            
            # 1) Prefer a live broadcast
            for item in response.get('items', []):
                if item.get('status', {}).get('lifeCycleStatus') == 'live':
                    return item

            # 2) Otherwise pick an upcoming one (ready/testing/created)
            for item in response.get('items', []):
                st = item.get('status', {}).get('lifeCycleStatus')
                if st in ('ready', 'testing', 'created'):
                    return item

            return None

        except HttpError as e:
            logging.error(f"Error getting broadcast: {e}")
            return None

    def update_broadcast_settings(self, title=None, description=None, privacy=None):
        """Update broadcast settings (title, description, privacy)"""
        if not self.service: return False
        
        try:
            broadcast = self.get_active_broadcast()
            if not broadcast:
                logging.warning("No active or upcoming broadcast found to update.")
                return False
            
            broadcast_id = broadcast['id']
            current_title = broadcast.get('snippet', {}).get('title')
            logging.info(f"Found Broadcast: {current_title} (ID: {broadcast_id})")

            body = {'id': broadcast_id}
            parts = []

            if title or description:
                parts.append('snippet')
                body['snippet'] = {}
                if title: body['snippet']['title'] = title
                if description: body['snippet']['description'] = description

            if privacy:
                parts.append('status')
                body['status'] = {'privacyStatus': privacy}

            if not parts:
                return True

            logging.info(f"Applying Updates -> Title: {title or '(unchanged)'}, Privacy: {privacy or '(unchanged)'}")
            
            self.service.liveBroadcasts().update(
                part=','.join(parts),
                body=body
            ).execute()

            logging.info("✓ Broadcast settings updated successfully.")
            return True
        except HttpError as e:
            logging.error(f"Error updating broadcast: {e}")
            return False

# --- END OF API CLASS ---

def load_config():
    """Loads stream key and metadata from JSON config."""
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"Config file not found at {CONFIG_FILE}")
        sys.exit(1)
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Could not read config: {e}")
        sys.exit(1)

# Load Config Globally
APP_CONFIG = load_config()
YOUTUBE_RTMP = f"rtmp://a.rtmp.youtube.com/live2/{APP_CONFIG['stream_key']}"

def log_subprocess(pipe, prefix):
    """Logs FFmpeg errors to console."""
    try:
        for line in iter(pipe.readline, b''):
            msg = line.decode().strip()
            if "frame=" not in msg and "speed=" not in msg:
                print(f"[{prefix}] {msg}")
    except: pass

def get_black_generator():
    """Generates Black Frames (MPEG-TS)."""
    cmd = [
        'ffmpeg', '-re', '-y', '-hide_banner', '-loglevel', 'warning',
        '-f', 'lavfi', '-i', f'color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(FPS*2),
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def get_live_generator():
    """Reads NGINX Stream (MPEG-TS)."""
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
        '-rw_timeout', '5000000',
        '-i', INPUT_RTMP,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(FPS*2),
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def start_sender_process():
    """Connects to Python TCP Server -> Sends to YouTube."""
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
        '-f', 'mpegts',
        '-fflags', '+genpts+igndts', # CRITICAL: Regenerate timestamps
        '-i', f'tcp://{RELAY_HOST}:{RELAY_PORT}?listen',
        '-c', 'copy', 
        '-f', 'flv', YOUTUBE_RTMP
    ]
    return subprocess.Popen(cmd, stderr=subprocess.PIPE)

def run_relay():
    # --- STEP 1: CONFIGURE YOUTUBE API ---
    logging.info("--- INITIALIZING RELAY ---")
    
    try:
        yt = YouTubeAPIHelper()
        if yt.service:
            yt.update_broadcast_settings(
                title=APP_CONFIG.get('broadcast_title'),
                description=APP_CONFIG.get('broadcast_description'),
                privacy=APP_CONFIG.get('privacy', 'public') # Default to public if missing
            )
        else:
            logging.warning("Skipping API updates due to auth failure.")
    except Exception as e:
        logging.error(f"Unexpected API Error: {e}")

    # --- STEP 2: START VIDEO RELAY ---
    logging.info(f"[*] TCP Relay Server Started on port {RELAY_PORT}")
    
    sender = start_sender_process()
    t = threading.Thread(target=log_subprocess, args=(sender.stderr, "SENDER"))
    t.daemon = True
    t.start()
    
    time.sleep(1) # Wait for Sender to listen

    try:
        conn = socket.create_connection((RELAY_HOST, RELAY_PORT))
        logging.info("[+] Connected to Sender Pipe")
    except ConnectionRefusedError:
        logging.error("[!] Failed to connect to Sender.")
        return

    current_source = None
    source_type = "NONE"
    
    try:
        while True:
            if sender.poll() is not None:
                logging.error("[!] Sender died! Restarting...")
                conn.close()
                sender = start_sender_process()
                time.sleep(1)
                conn = socket.create_connection((RELAY_HOST, RELAY_PORT))

            # --- SOURCE SWITCHING ---
            if source_type != "LIVE":
                live_test = get_live_generator()
                time.sleep(0.5)
                
                if live_test.poll() is None:
                    logging.info("[+] LIVE STREAM DETECTED! Switching...")
                    if current_source: current_source.kill()
                    current_source = live_test
                    source_type = "LIVE"
                else:
                    live_test.kill()
                    if source_type != "BLACK":
                        logging.info("[-] Source Lost. Switching to BLACK frames.")
                        if current_source: current_source.kill()
                        current_source = get_black_generator()
                        source_type = "BLACK"

            # --- DATA PUMP ---
            try:
                data = current_source.stdout.read(65536)
                if not data:
                    source_type = "NONE"
                    continue
                conn.sendall(data)
            except (BrokenPipeError, ConnectionResetError):
                time.sleep(1)

    except KeyboardInterrupt:
        logging.info("Stopping...")
    finally:
        if current_source: current_source.kill()
        sender.kill()
        conn.close()

if __name__ == "__main__":
    run_relay()
