import subprocess
import sys
import time
import threading
import os

# ================= CONFIGURATION =================
# 1. INPUT: Your local NGINX-RTMP stream
INPUT_RTMP = "rtmp://localhost:1935/live/berkshire"

# 2. OUTPUT: Your YouTube RTMP URL + Key
YOUTUBE_RTMP = "rtmp://a.rtmp.youtube.com/live2/pyby-fj4b-9j7j-9r3e-0ueq"

# 3. STREAM SPECS (Must match your Wirecast settings)
WIDTH = 1920
HEIGHT = 1080
FPS = 30
# =================================================

def log_stream(pipe, prefix):
    """Logs stderr from FFmpeg to console for debugging."""
    try:
        for line in iter(pipe.readline, b''):
            print(f"[{prefix}] {line.decode().strip()}")
    except ValueError:
        pass

def get_sender_cmd():
    """
    The Persistent Sender.
    Accepts MPEG-TS stream from STDIN.
    Ignores input timestamps and generates new ones (CRITICAL for switching).
    """
    cmd = [
        'ffmpeg',
        '-y',
        '-hide_banner',
        '-loglevel', 'error',       # Reduce spam, show only errors
        
        # INPUT SETTINGS
        '-thread_queue_size', '1024',
        '-f', 'mpegts',             # Robust container for splicing
        '-fflags', '+genpts+igndts', # IGNORE input timestamps, generate new ones
        '-i', 'pipe:0',             # Read from Python
        
        # OUTPUT ENCODING (YouTube Recommended)
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-maxrate', '4500k',
        '-bufsize', '9000k',
        '-pix_fmt', 'yuv420p',
        '-g', str(FPS * 2),         # Keyframe every 2s
        
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ar', '44100',
        
        '-f', 'flv',
        YOUTUBE_RTMP
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

def get_live_source_cmd():
    """
    Reads NGINX stream and outputs MPEG-TS.
    """
    cmd = [
        'ffmpeg',
        '-y',
        '-hide_banner',
        '-loglevel', 'error',
        '-i', INPUT_RTMP,
        
        # Normalize to MPEG-TS for piping
        '-c:v', 'libx264',          # Pre-encode to x264 for consistency
        '-preset', 'ultrafast',     # Fast encode for intermediate
        '-g', str(FPS),             # Frequent keyframes for fast switching
        '-s', f'{WIDTH}x{HEIGHT}',
        '-r', str(FPS),
        
        '-c:a', 'aac',
        '-ar', '44100',
        '-ac', '2',
        
        '-f', 'mpegts',
        'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def get_black_source_cmd():
    """
    Generates Black Frames in MPEG-TS format.
    """
    cmd = [
        'ffmpeg',
        '-y',
        '-hide_banner',
        '-loglevel', 'error',
        '-re',                      # Read at native speed (Don't flood memory)
        '-f', 'lavfi', '-i', f'color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-g', str(FPS),
        
        '-c:a', 'aac',
        '-ar', '44100',
        '-ac', '2',
        
        '-f', 'mpegts',
        'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def run_relay():
    print(f"[*] Relay V3 (MPEG-TS Mode) Started")
    print(f"[*] Target: {YOUTUBE_RTMP}")

    sender = get_sender_cmd()
    
    # Logging thread for Sender
    t = threading.Thread(target=log_stream, args=(sender.stderr, "SENDER"))
    t.daemon = True
    t.start()

    current_source = None
    source_type = "NONE"
    
    # 32KB Buffer - small enough to stay responsive, large enough for efficiency
    CHUNK_SIZE = 32768 

    try:
        while True:
            if sender.poll() is not None:
                print("\n[!] FATAL: Sender process died. Check Stream Key.")
                break

            # --- 1. CHECK / SWITCH SOURCE ---
            if source_type != "LIVE":
                # Try to peek at NGINX
                # We do this by launching the process; if it dies immediately, stream is down.
                test_live = get_live_source_cmd()
                time.sleep(0.2) # tiny buffer to let it fail if offline
                
                if test_live.poll() is None:
                    print("\n[+] LIVE STREAM DETECTED. Switching...")
                    if current_source: current_source.terminate()
                    current_source = test_live
                    source_type = "LIVE"
                    
                    # Log Live errors non-blocking
                    lt = threading.Thread(target=log_stream, args=(current_source.stderr, "LIVE"))
                    lt.daemon = True
                    lt.start()
                else:
                    test_live.kill()
                    # If we aren't already playing black, start it
                    if source_type != "BLACK":
                        print("\n[-] Source Offline. Switching to BLACK frames.")
                        if current_source: current_source.terminate()
                        current_source = get_black_source_cmd()
                        source_type = "BLACK"

            # --- 2. RELAY DATA ---
            try:
                data = current_source.stdout.read(CHUNK_SIZE)
                
                if not data:
                    # Current source dried up
                    print(f"\n[!] {source_type} source ended. Re-evaluating.")
                    source_type = "NONE" # Force loop to re-check
                    continue
                
                sender.stdin.write(data)
                sender.stdin.flush()

            except (BrokenPipeError, OSError):
                print("\n[!] Pipe broken. Restarting Sender...")
                sender.kill()
                sender = get_sender_cmd()
                t = threading.Thread(target=log_stream, args=(sender.stderr, "SENDER"))
                t.daemon = True
                t.start()

    except KeyboardInterrupt:
        print("\n[*] Stopping...")
    finally:
        if sender: sender.kill()
        if current_source: current_source.kill()

if __name__ == "__main__":
    run_relay()
