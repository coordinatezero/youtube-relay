import subprocess
import sys
import time
import select
import threading

# ================= CONFIGURATION =================
# 1. INPUT: Your local NGINX-RTMP stream
INPUT_RTMP = "rtmp://localhost:1935/live/berkshire"

# 2. OUTPUT: Your YouTube RTMP URL + Key
#    Example: rtmp://a.rtmp.youtube.com/live2/abcd-1234-efgh-5678
YOUTUBE_RTMP = "rtmp://a.rtmp.youtube.com/live2/pyby-fj4b-9j7j-9r3e-0ueq"

# 3. STREAM SETTINGS
#    These MUST match your Wirecast output exactly to avoid scaling issues.
WIDTH = 1920
HEIGHT = 1080
FPS = 30
# =================================================

def log_subprocess_output(pipe, prefix):
    """Reads stderr from a subprocess and logs it (prevents buffer blocking)."""
    try:
        for line in iter(pipe.readline, b''):
            print(f"[{prefix}] {line.decode().strip()}")
    except ValueError:
        pass

def get_sender_cmd():
    """
    The Persistent Sender.
    Reads a NUT stream (Raw Video + PCM Audio) from STDIN.
    Encodes to H.264/AAC and sends to YouTube.
    """
    cmd = [
        'ffmpeg',
        '-y',
        '-f', 'nut',                # Input Container: NUT (Pipe friendly)
        '-i', 'pipe:0',             # Read from Python Stdin
        
        # ENCODING SETTINGS (CPU Usage vs Quality)
        '-c:v', 'libx264',
        '-preset', 'veryfast',      # Use 'ultrafast' if CPU is high
        '-maxrate', '4500k',        # Bitrate Cap
        '-bufsize', '9000k',
        '-pix_fmt', 'yuv420p',
        '-g', str(FPS * 2),         # Keyframe every 2s (YouTube requirement)
        
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ar', '44100',
        
        '-f', 'flv',                # RTMP Container
        YOUTUBE_RTMP
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

def get_live_source_cmd():
    """
    Reads from NGINX-RTMP.
    Decodes to Raw Video/Audio packed in a NUT container.
    """
    cmd = [
        'ffmpeg',
        '-y',
        '-i', INPUT_RTMP,
        
        # Output Raw Data in NUT container
        '-f', 'nut',
        '-c:v', 'rawvideo',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'pcm_s16le',
        '-ar', '44100',
        '-ac', '2',
        'pipe:1'
    ]
    # stderr=subprocess.DEVNULL prevents console spam, change to PIPE to debug
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

def get_black_source_cmd():
    """
    Generates Black Video + Silence.
    Uses -re to simulate real-time speed so we don't overflow buffers.
    """
    cmd = [
        'ffmpeg',
        '-y',
        '-re',                      # READ AT NATIVE SPEED (Crucial for generator)
        '-f', 'lavfi', '-i', f'color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}',
        '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
        
        '-f', 'nut',
        '-c:v', 'rawvideo',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'pcm_s16le',
        '-shortest',                # End when the shortest input ends (infinite anyway)
        'pipe:1'
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

def run_relay():
    print("--- ROBUST RELAY V2 STARTING ---")
    print(f"Input:  {INPUT_RTMP}")
    print(f"Output: {YOUTUBE_RTMP}")
    
    # Start the Sender (Connection to YouTube)
    sender = get_sender_cmd()
    
    # Start a thread to log Sender errors (Vital for debugging "No Output")
    log_thread = threading.Thread(target=log_subprocess_output, args=(sender.stderr, "SENDER"))
    log_thread.daemon = True
    log_thread.start()

    current_source = None
    source_type = "NONE" # "LIVE" or "BLACK"
    
    # Buffer size: 64KB chunks
    CHUNK_SIZE = 65536 

    try:
        while True:
            # Check if Sender died
            if sender.poll() is not None:
                print("\n[!] Sender Process Died! YouTube connection lost.")
                break

            # --- STATE MACHINE ---
            
            # 1. Try to connect to LIVE source if not already connected
            if source_type != "LIVE":
                # Check if NGINX stream is available by trying to open it
                # A quick probe could be added here, but simply trying to run FFmpeg is robust
                # We start a 'probe' process. 
                print("[*] Checking for Live Stream...")
                live_proc = get_live_source_cmd()
                
                # Give it 1 second to produce data
                time.sleep(1)
                if live_proc.poll() is None:
                    # It's running! Switch to Live.
                    if current_source: current_source.terminate()
                    current_source = live_proc
                    source_type = "LIVE"
                    print("\n[+] SWITCHED TO LIVE SOURCE")
                else:
                    # It failed immediately (stream offline), kill it and fallback
                    live_proc.kill()
            
            # 2. If we are waiting for live, ensure we have a BLACK source running
            if source_type != "LIVE" and source_type != "BLACK":
                if current_source: current_source.terminate()
                current_source = get_black_source_cmd()
                source_type = "BLACK"
                print("\n[-] SWITCHED TO BLACK FRAME GENERATOR")

            # 3. Verify current source is still producing data
            if current_source.poll() is not None:
                print(f"\n[!] {source_type} source ended unexpectedly. Resetting...")
                source_type = "NONE" # Force re-evaluation
                continue

            # --- DATA PUMP ---
            try:
                # Read a chunk
                data = current_source.stdout.read(CHUNK_SIZE)
                
                if not data:
                    # End of stream (Wirecast disconnected)
                    print(f"\n[!] {source_type} stream EOF.")
                    source_type = "NONE"
                    continue
                
                # Write to Sender
                try:
                    sender.stdin.write(data)
                    sender.stdin.flush()
                except BrokenPipeError:
                    print("[!] Sender Pipe Broken. Exiting.")
                    break
                    
            except Exception as e:
                print(f"[!] I/O Error: {e}")
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n[*] Stopping Relay...")
    finally:
        if sender: sender.terminate()
        if current_source: current_source.terminate()

if __name__ == "__main__":
    run_relay()
