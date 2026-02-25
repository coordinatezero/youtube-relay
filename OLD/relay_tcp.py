import subprocess
import socket
import time
import threading
import sys
import os

# ================= CONFIGURATION =================
# 1. INPUT: Your local NGINX-RTMP stream
INPUT_RTMP = "rtmp://localhost:1935/live/stream"

# 2. OUTPUT: Your YouTube RTMP URL + Key
#    Combine them: rtmp://a.rtmp.youtube.com/live2/YOUR-KEY
YOUTUBE_RTMP = "rtmp://a.rtmp.youtube.com/live2/YOUR-STREAM-KEY"

# 3. INTERNAL PORTS (Local only)
RELAY_HOST = "127.0.0.1"
RELAY_PORT = 9999

# 4. STREAM SPECS (MUST match Wirecast)
WIDTH = 1920
HEIGHT = 1080
FPS = 30
# =================================================

def log_subprocess(pipe, prefix):
    """Reads stderr from FFmpeg and logs it to console."""
    try:
        for line in iter(pipe.readline, b''):
            msg = line.decode().strip()
            if "speed=" in msg: continue # Skip progress bars
            print(f"[{prefix}] {msg}")
    except: pass

def get_black_generator():
    """Generates MPEG-TS Black Frames + Silence."""
    cmd = [
        'ffmpeg', '-re', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'lavfi', '-i', f'color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(FPS),
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def get_live_generator():
    """Reads from NGINX and outputs MPEG-TS."""
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-rw_timeout', '5000000', # 5s Timeout if stream dead
        '-i', INPUT_RTMP,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', str(FPS),
        '-c:a', 'aac', '-ar', '44100', '-ac', '2',
        '-f', 'mpegts', 'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def start_sender_process():
    """
    Connects to Python TCP Server and relays to YouTube.
    Retries automatically if connection fails.
    """
    cmd = [
        'ffmpeg', '-y', '-hide_banner',
        '-analyzeduration', '1000000', # Low analyze time for fast start
        '-probesize', '1000000',
        '-f', 'mpegts',                # Input is MPEG-TS from TCP
        '-i', f'tcp://{RELAY_HOST}:{RELAY_PORT}', 
        
        # ENCODING FOR YOUTUBE
        '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '4500k',
        '-g', str(FPS*2),              # Keyframe every 2s
        '-c:a', 'aac', '-b:a', '128k',
        '-f', 'flv', YOUTUBE_RTMP
    ]
    return subprocess.Popen(cmd, stderr=subprocess.PIPE)

def run_server():
    print(f"[*] Starting TCP Relay Server on {RELAY_HOST}:{RELAY_PORT}...")
    
    # 1. Start the TCP Server
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((RELAY_HOST, RELAY_PORT))
    server_sock.listen(1)

    # 2. Start the Sender (It will connect to us)
    print("[*] Launching FFmpeg Sender...")
    sender = start_sender_process()
    
    # Log Sender Errors
    t = threading.Thread(target=log_subprocess, args=(sender.stderr, "SENDER"))
    t.daemon = True
    t.start()

    # 3. Accept Connection
    print(f"[*] Waiting for Sender to connect...")
    conn, addr = server_sock.accept()
    print(f"[+] Sender Connected from {addr}")

    current_source = None
    source_type = "NONE"
    
    try:
        while True:
            # Monitor Sender Health
            if sender.poll() is not None:
                print("[!] Sender died. Restarting...")
                conn.close()
                sender = start_sender_process()
                conn, addr = server_sock.accept()
                print("[+] Sender Re-Connected")

            # --- SOURCE SELECTION LOGIC ---
            if source_type != "LIVE":
                # Attempt to switch to Live
                test_live = get_live_generator()
                time.sleep(0.5) # Give it a moment to buffer
                if test_live.poll() is None:
                    print("\n[+] SWITCHING TO LIVE STREAM")
                    if current_source: current_source.kill()
                    current_source = test_live
                    source_type = "LIVE"
                else:
                    # Live failed, ensure we are on Black
                    test_live.kill()
                    if source_type != "BLACK":
                        print("\n[-] INPUT LOST. SWITCHING TO BLACK FRAMES.")
                        if current_source: current_source.kill()
                        current_source = get_black_generator()
                        source_type = "BLACK"
            
            # --- DATA PUMP ---
            try:
                # Read 32kb chunks
                data = current_source.stdout.read(32768)
                
                if not data:
                    # Source dried up
                    print(f"\n[!] {source_type} EOF.")
                    source_type = "NONE" # Force re-evaluation
                    continue
                
                # Send to FFmpeg over TCP
                conn.sendall(data)

            except (BrokenPipeError, ConnectionResetError):
                print("[!] TCP Connection Broken.")
                # Allow loop to restart sender
                pass

    except KeyboardInterrupt:
        print("\n[*] Stopping...")
    finally:
        if current_source: current_source.kill()
        sender.kill()
        server_sock.close()

if __name__ == "__main__":
    run_server()
