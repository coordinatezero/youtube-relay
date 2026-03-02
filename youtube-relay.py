#!/usr/bin/env python3
# version 6
# coordinatezero@gmail.com

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

# Basic Config
CONFIG_FILE = "/etc/youtube-relay/config.json"
PICKLE_FILE = '/etc/youtube-relay/token.pickle'

# INTERNAL SETTINGS
RELAY_HOST = "127.0.0.1"
RELAY_PORT = 10000
WIDTH = 1920
HEIGHT = 1080

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
try:
    INPUT_RTMP = APP_CONFIG.get('input_rtmp')
    YOUTUBE_RTMP = f"rtmp://a.rtmp.youtube.com/live2/{APP_CONFIG.get('stream_key')}"
    FPS = int(APP_CONFIG.get('fps'))
except Exception as e:
    logging.error(f"Missing config options: {e}")
    exit

class YouTubeAPIHelper:
    def __init__(self):
        self.service = None
        self.authenticate()

    def authenticate(self):
        creds = None
        if not os.path.exists(PICKLE_FILE):
            logging.error(f"Token not found at {PICKLE_FILE}")
            return
        try:
            with open(PICKLE_FILE, 'rb') as token:
                creds = pickle.load(token)
        except Exception as e:
            logging.error("Failed to load token pickle: %r", e)
            return

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(PICKLE_FILE, 'wb') as token: pickle.dump(creds, token)
            except Exception as e:
                logging.error("Token refresh failed: %r", e)
                return

        if creds and creds.valid:
            try:
                self.service = build('youtube', 'v3', credentials=creds, cache_discovery=False)
                logging.info("YouTube API service initialised")
            except Exception as e:
                logging.error("YouTube API build() failed: %r", e)
                self.service = None
        else:
            logging.error("Token loaded but not valid and not refreshable (no refresh_token?)")
            self.service = None

    def update_broadcast(self):
        if not self.service:
            logging.info("not self.service")
            return
        try:
            req = self.service.liveBroadcasts().list(part='id,snippet,status,contentDetails', broadcastType='all', mine=True, maxResults=5)
            resp = req.execute()
            logging.info("Broadcast candidates: %s", [
                (i.get('id'),
                 i.get('snippet', {}).get('title'),
                 i.get('status', {}).get('lifeCycleStatus'),
                 i.get('status', {}).get('privacyStatus'))
                for i in resp.get('items', [])
            ])
            broadcast = next((i for i in resp.get('items', []) if i.get('status', {}).get('lifeCycleStatus') == 'live'), None)
            if not broadcast:
                broadcast = next((i for i in resp.get('items', []) if i.get('status', {}).get('lifeCycleStatus') in ('ready','created')), None)
            
            if broadcast:
                bid = broadcast['id']
                logging.info(f"Updating Broadcast: {broadcast.get('snippet',{}).get('title')} ({bid})")
                details = self.service.liveBroadcasts().list(
                    part='contentDetails',
                    id=bid
                ).execute()
                content_details = details['items'][0].get('contentDetails', {})
                content_details['enableDvr'] = False
                self.service.liveBroadcasts().update(
                    part='snippet,status,contentDetails',
                    body={
                        'id': bid,
                        'snippet': {
                            'title': APP_CONFIG.get('broadcast_title', 'Live Stream'),
                            'description': APP_CONFIG.get('broadcast_description', '')
                        },
                        'status': {
                            'privacyStatus': APP_CONFIG.get('privacy', 'public')
                        },
                        'contentDetails': content_details
                    }
                ).execute()
                logging.info("✓ Broadcast settings updated.")
                return bid
        except Exception as e:
            logging.error(f"API Update Failed: {e}")
        return None

def log_stream(pipe, prefix):
    """
    Reads a pipe in a thread to prevent blocking and logs errors.
    """
    try:
        for line in iter(pipe.readline, b''):
            msg = line.decode().strip()
            # Filter for specific NGINX errors
            if "Server returned 404" in msg:
                logging.error(f"[{prefix}] 404 Error! NGINX cannot find stream '{INPUT_RTMP}'")
                ACSK = APP_CONFIG['stream_key']
                logging.error(f"[{prefix}] Verify external app is streaming to 'rtmp://.../live' with key '{ACSK}'")
            elif "Connection refused" in msg:
                 logging.warning(f"[{prefix}] Connection Refused. Is NGINX running?")
    except: pass

def get_black_generator():
    """
    generate black frames, forces CFR and a 2-second GOP (keyframes every 2s)
    """
    cmd = [
        'ffmpeg', '-nostdin', '-re', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'color=c=black:s=%dx%d:r=%d' % (WIDTH, HEIGHT, FPS),
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-r', str(FPS),
        '-vsync', 'cfr',
        '-c:v', 'libx264', '-preset', 'ultrafast',
        '-b:v', '6000k',
        '-maxrate', '6800k',
        '-bufsize', '13600k',
        '-g', str(FPS * 2),
        '-keyint_min', str(FPS * 2),
        '-sc_threshold', '0',
        '-force_key_frames', 'expr:gte(t,n_forced*2)',
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

def get_live_generator():
    """
    Attempts to read from NGINX, Force CFR and a 2-second GOP (keyframes every 2s)
    """
    cmd = [
        'ffmpeg', '-nostdin', '-re', '-y', '-hide_banner', '-loglevel', 'info',
        '-rw_timeout', '5000000',  # 5s Timeout
        '-i', INPUT_RTMP,
        '-r', str(FPS),
        '-vsync', 'cfr',
        '-c:v', 'libx264', '-preset', 'ultrafast',
        '-b:v', '6000k',
        '-maxrate', '6800k',
        '-bufsize', '13600k',
        '-g', str(FPS * 2),
        '-keyint_min', str(FPS * 2),
        '-sc_threshold', '0',
        '-force_key_frames', 'expr:gte(t,n_forced*2)',
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

def is_live_present():
    cmd = [
        'ffprobe', '-v', 'error',
        '-rw_timeout', '2000000',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=codec_name',
        '-of', 'default=nw=1:nk=1',
        INPUT_RTMP
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.returncode == 0 and p.stdout.strip() != b''

def run_relay():
    logging.info("YouTube Relay")
    logging.info(f"[*] Listening for Input: {INPUT_RTMP}")
    logging.info(f"[*] Target: YouTube")

    yt = YouTubeAPIHelper()

    # start the sender
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
                if is_live_present():
                    logging.info(">>> DETECTED EXTERNAL STREAM <<<")
                    if current_source:
                        current_source.kill()
                        current_source.wait()
                        current_source = None
                    current_source = get_live_generator()
                    source_type = "LIVE"

                    # Set public + DVR off at the moment live input appears
                    try:
                        APP_CONFIG['privacy'] = 'public'
                        bid = yt.update_broadcast()
                        if bid:
                            chk = yt.service.liveBroadcasts().list(part='status', id=bid).execute()
                            privacy = chk['items'][0]['status'].get('privacyStatus')
                            logging.info("Post-update privacyStatus for %s: %s", bid, privacy)
                    except Exception as e:
                        logging.info("[!] Couldn't update broadcast: %s", e)
                    
                    # Log stderr to catch mid-stream errors
                    threading.Thread(target=log_stream, args=(current_source.stderr, "LIVE_IN"), daemon=True).start()
                else:
                    msg = ""
                    if current_source and current_source.stderr:
                        msg = current_source.stderr.read(4096).decode('utf-8', errors='ignore').strip()
                    if msg:
                        logging.info("Live stream ended: %s", msg)
                        if "Server returned 404" in msg:
                            logging.info("NGINX 404: Stream key mismatch. Expecting: %s", INPUT_RTMP)
                    else:
                        logging.info("Live stream ended")

                    if source_type != "BLACK":
                        logging.info("[-] Input Offline. Sending Black Frames.")
                        if current_source:
                            current_source.kill()
                            current_source.wait()
                            current_source = None
                        current_source = get_black_generator()
                        source_type = "BLACK"

            # Data Pump
            try:
                data = current_source.stdout.read(65536)
                if not data:
                    if source_type == "LIVE":
                        logging.info("Live Stream Ended (EOF)")
                    if current_source:
                        current_source.kill()
                        current_source.wait()
                        current_source = None
                    source_type = "NONE"
                    continue
                conn.sendall(data)
            except Exception:
                time.sleep(0.1)

    except KeyboardInterrupt:
        logging.info("Caught Ctrl-C, exiting...")
    except Exception as e:
        logging.info("Fatal error: %r", e)
    finally:
        # cleanup exactly as you already do
        if current_source:
            current_source.kill()
            current_source.wait()
        if sender:
            sender.kill()
            sender.wait()
        if conn:
            conn.close()
        subprocess.run(['stty', 'sane'], check=False)


if __name__ == "__main__":
    run_relay()
