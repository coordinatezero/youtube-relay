#!/usr/bin/env python3

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
CONFIG_FILE = "/etc/youtube-relay/config.json"
TOKEN_PATH = '/etc/youtube-relay/token.pickle'

# CORRECTION: Updated Default Input to match your Wirecast stream key
DEFAULT_INPUT_RTMP = "rtmp://localhost:1935/live/berkshire"

# INTERNAL SETTINGS
RELAY_HOST = "127.0.0.1"
RELAY_PORT = 10000
WIDTH = 1920
HEIGHT = 1080
FPS = 15

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(asctime)s %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
# =================================================

def load_config():
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"Config file not found at {CONFIG_FILE}")
        sys.exit(1)
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Could not read config: {e}")
        sys.exit(1)

APP_CONFIG = load_config()
# Prefer config file 'input_rtmp', otherwise use the default (berkshire)
INPUT_RTMP = APP_CONFIG.get('input_rtmp', DEFAULT_INPUT_RTMP)
YOUTUBE_RTMP = f"rtmp://a.rtmp.youtube.com/live2/{APP_CONFIG['stream_key']}"

# --- API HELPER ---
class YouTubeAPIHelper:
    def __init__(self):
        self.service = None
        self.authenticate()

    def authenticate(self):
        creds = None
        if not os.path.exists(TOKEN_PATH):
            logging.error(f"Token not found at {TOKEN_PATH}")
            return
        try:
            with open(TOKEN_PATH, 'rb') as token:
                creds = pickle.load(token)
        except Exception:
            return

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_PATH, 'wb') as token: pickle.dump(creds, token)
            except: return

        if creds and creds.valid:
            try:
                self.service = build('youtube', 'v3', credentials=creds, cache_discovery=False)
            except: pass

    def update_broadcast(self):
        if not self.service: return
        try:
            req = self.service.liveBroadcasts().list(part='id,snippet,status', broadcastType='all', mine=True, maxResults=5)
            resp = req.execute()
            broadcast = next((i for i in resp.get('items', []) if i.get('status', {}).get('lifeCycleStatus') == 'live'), None)
            if not broadcast:
                broadcast = next((i for i in resp.get('items', []) if i.get('status', {}).get('lifeCycleStatus') in ('ready','created')), None)
            
            if broadcast:
                bid = broadcast['id']
                logging.info(f"Updating Broadcast: {broadcast.get('snippet',{}).get('title')} ({bid})")
                self.service.liveBroadcasts().update(
                    part='snippet,status',
                    body={
                        'id': bid,
                        'snippet': {
                            'title': APP_CONFIG.get('broadcast_title', 'Live Stream'),
                            'description': APP_CONFIG.get('broadcast_description', '')
                        },
                        'status': {'privacyStatus': APP_CONFIG.get('privacy', 'public')}
                    }
                ).execute()
                logging.info("✓ Broadcast settings updated.")
        except Exception as e:
            logging.error(f"API Update Failed: {e}")

# --- FFmpeg HELPERS ---

def log_stream(pipe, prefix):
    """Reads a pipe in a thread to prevent blocking and logs errors."""
    try:
        for line in iter(pipe.readline, b''):
            msg = line.decode().strip()
            # Filter for specific NGINX errors
            if "Server returned 404" in msg:
                logging.error(f"[{prefix}] 404 Error! NGINX cannot find stream '{INPUT_RTMP}'")
                logging.error(f"[{prefix}] Verify Wirecast is streaming to 'rtmp://.../live' with key 'berkshire'")
            elif "Connection refused" in msg:
                 logging.warning(f"[{prefix}] Connection Refused. Is NGINX running?")
    except: pass

def get_black_generator():
    cmd = [
        'ffmpeg', '-re', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'lavfi', '-i', f'color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(FPS*2),
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

def get_live_generator():
    """
    Attempts to read from NGINX. 
    """
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'info', 
        '-rw_timeout', '5000000', # 5s Timeout
        '-i', INPUT_RTMP,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(FPS*2),
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def start_sender():
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'mpegts', '-fflags', '+genpts+igndts',
        '-i', f'tcp://{RELAY_HOST}:{RELAY_PORT}?listen',
        '-c', 'copy', '-f', 'flv', YOUTUBE_RTMP
    ]
    return subprocess.Popen(cmd, stderr=subprocess.PIPE)

# --- MAIN LOOP ---

def run_relay():
    logging.info("--- RELAY V6 (Berkshire Edition) ---")
    logging.info(f"[*] Listening for Input: {INPUT_RTMP}")
    logging.info(f"[*] Target: YouTube")

    # 1. Run API Update
    try:
        YouTubeAPIHelper().update_broadcast()
    except Exception: pass 

    # 2. Start Sender
    sender = start_sender()
    threading.Thread(target=log_stream, args=(sender.stderr, "SENDER"), daemon=True).start()
    time.sleep(1)

    try:
        conn = socket.create_connection((RELAY_HOST, RELAY_PORT))
    except Exception as e:
        logging.error(f"Sender connection failed: {e}")
        return

    current_source = None
    source_type = "NONE"
    
    try:
        while True:
            # Monitor Sender
            if sender.poll() is not None:
                logging.error("Sender died. Restarting...")
                conn.close()
                sender = start_sender()
                time.sleep(1)
                conn = socket.create_connection((RELAY_HOST, RELAY_PORT))

            # Check for Live Stream
            if source_type != "LIVE":
                # Launch a probe process
                live_test = get_live_generator()
                time.sleep(0.5)
                
                if live_test.poll() is None:
                    logging.info(">>> DETECTED WIRECAST STREAM <<<")
                    if current_source: current_source.kill()
                    current_source = live_test
                    source_type = "LIVE"
                    
                    # Log stderr to catch mid-stream errors
                    threading.Thread(target=log_stream, args=(current_source.stderr, "LIVE_IN"), daemon=True).start()
                else:
                    # Check why it failed
                    err = live_test.stderr.read().decode('utf-8', errors='ignore')
                    live_test.kill()

                    if "Server returned 404" in err:
                        logging.warning(f"NGINX 404: Stream key mismatch. Expecting: {INPUT_RTMP}")
                    
                    if source_type != "BLACK":
                        logging.info("[-] Input Offline. Sending Black Frames.")
                        if current_source: current_source.kill()
                        current_source = get_black_generator()
                        source_type = "BLACK"

            # Data Pump
            try:
                data = current_source.stdout.read(65536)
                if not data:
                    if source_type == "LIVE": logging.info("Live Stream Ended (EOF)")
                    source_type = "NONE"
                    continue
                conn.sendall(data)
            except:
                time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        if current_source: current_source.kill()
        sender.kill()
        conn.close()

if __name__ == "__main__":
    run_relay()
