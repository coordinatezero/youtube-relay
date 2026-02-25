import subprocess
import sys
import time
import os
import select

# ================= CONFIGURATION =================
# 1. INPUT: Your local NGINX-RTMP stream (Wirecast sends here)
#    Replace 'live' and 'stream' with your NGINX app/key
INPUT_RTMP = "rtmp://abunai.com:1935/live/berkshire"

# 2. OUTPUT: Your YouTube RTMP URL
#    Replace with your actual YouTube URL + Key
YOUTUBE_RTMP = "rtmp://a.rtmp.youtube.com/live2/pyby-fj4b-9j7j-9r3e-0ueq"

# 3. Stream Specs (MUST match Wirecast output to avoid scaling artifacts)
WIDTH = 1920
HEIGHT = 1080
FPS = 15
AUDIO_RATE = 48000
CHANNELS = 2

# Internal buffer settings
VIDEO_FRAME_SIZE = WIDTH * HEIGHT * 3  # Raw RGB24 frame size
AUDIO_FRAME_SIZE = 4096  # Chunk size for audio buffering
# =================================================

def get_sender_cmd():
    """
    Starts the persistent FFmpeg process that sends to YouTube.
    It listens for RAW VIDEO on Pipe 0 (Stdin) and RAW AUDIO on a Named Pipe.
    """
    audio_fifo = "/tmp/audio_pipe_in"
    if not os.path.exists(audio_fifo):
        os.mkfifo(audio_fifo)

    return subprocess.Popen([
        'ffmpeg',
        '-re', '-y',
        
        # INPUT 1: Raw Video (from Python Stdin)
        '-f', 'rawvideo', '-vcodec', 'rawvideo', '-pix_fmt', 'rgb24',
        '-s', f'{WIDTH}x{HEIGHT}', '-r', str(FPS), '-i', '-',
        
        # INPUT 2: Raw Audio (from Named Pipe)
        '-f', 's16le', '-ac', str(CHANNELS), '-ar', str(AUDIO_RATE), 
        '-i', audio_fifo,

        # OUTPUT: Encoding settings for YouTube
        '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '4000k', '-g', str(FPS*2),
        '-c:a', 'aac', '-b:a', '128k',
        '-f', 'flv', YOUTUBE_RTMP
    ], stdin=subprocess.PIPE)

def get_receiver_cmd():
    """
    Starts the FFmpeg process that READS your NGINX stream.
    It splits output: Video -> Stdout, Audio -> Named Pipe.
    """
    audio_fifo = "/tmp/audio_pipe_out"
    if not os.path.exists(audio_fifo):
        os.mkfifo(audio_fifo)

    # Note: We use a pipe for audio output to keep stdout clean for video
    return subprocess.Popen([
        'ffmpeg', '-y',
        '-i', INPUT_RTMP,
        
        # Output Video to STDOUT
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-r', str(FPS), '-',
        
        # Output Audio to Named Pipe
        '-f', 's16le', '-ac', str(CHANNELS), '-ar', str(AUDIO_RATE), audio_fifo
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

def generate_black_frame():
    return bytes([0] * VIDEO_FRAME_SIZE)

def generate_silence(size):
    return bytes([0] * size)

def run_relay():
    print(f"[*] Starting Keep-Alive Relay...")
    print(f"[*] Source: {INPUT_RTMP}")
    print(f"[*] Target: YouTube")

    # Setup FIFOs
    audio_in_fifo_path = "/tmp/audio_pipe_in"
    audio_out_fifo_path = "/tmp/audio_pipe_out"
    if not os.path.exists(audio_in_fifo_path): os.mkfifo(audio_in_fifo_path)
    if not os.path.exists(audio_out_fifo_path): os.mkfifo(audio_out_fifo_path)

    sender = get_sender_cmd()
    
    # Open the audio pipe to the sender (Non-blocking open is tricky in Python, 
    # so we open it in read/write mode to prevent blocking)
    sender_audio = os.open(audio_in_fifo_path, os.O_WRONLY)
    
    receiver = None
    receiver_audio_fd = None
    
    black_frame = generate_black_frame()
    
    try:
        while True:
            # Check if Receiver (NGINX Source) is alive
            if receiver is None or receiver.poll() is not None:
                print(f"[*] Connecting to NGINX source...", end='\r')
                receiver = get_receiver_cmd()
                # Give it a moment to connect
                time.sleep(0.5)
                if receiver.poll() is None:
                     # Open the receiver's audio output pipe
                    receiver_audio_fd = os.open(audio_out_fifo_path, os.O_RDONLY | os.O_NONBLOCK)
                    print("\n[*] Source Connected! Relaying...")
                else:
                    # Source dead, ensure we reset
                    receiver = None

            # === READ/WRITE LOOP ===
            if receiver and receiver.poll() is None:
                # -- CASE A: STREAM IS LIVE --
                # Read Video
                video_data = receiver.stdout.read(VIDEO_FRAME_SIZE)
                if not video_data:
                    print("\n[!] Source disconnected (EOF). Switching to Black.")
                    receiver.terminate()
                    receiver = None
                    continue
                
                # Write Video
                try:
                    sender.stdin.write(video_data)
                except BrokenPipeError:
                    print("[!] Sender crashed. Restarting...")
                    sender = get_sender_cmd()
                    sender_audio = os.open(audio_in_fifo_path, os.O_WRONLY)

                # Handle Audio (Non-blocking read from FIFO)
                try:
                    audio_data = os.read(receiver_audio_fd, AUDIO_FRAME_SIZE)
                    if audio_data:
                        os.write(sender_audio, audio_data)
                    else:
                        # If no audio but video is live, send silence to keep sync
                        os.write(sender_audio, generate_silence(AUDIO_FRAME_SIZE))
                except BlockingIOError:
                    os.write(sender_audio, generate_silence(AUDIO_FRAME_SIZE))
                
            else:
                # -- CASE B: STREAM IS DEAD --
                # Send Black Frame
                try:
                    sender.stdin.write(black_frame)
                    # Send Silence (Critical to keep stream healthy)
                    os.write(sender_audio, generate_silence(44100 // FPS * 4)) # Approx 1 frame of audio
                    
                    # Throttle to match FPS (Prevent CPU spike)
                    time.sleep(1 / FPS)
                except BrokenPipeError:
                    sender = get_sender_cmd()
                    sender_audio = os.open(audio_in_fifo_path, os.O_WRONLY)

    except KeyboardInterrupt:
        print("\n[*] Stopping...")
        if receiver: receiver.terminate()
        sender.terminate()
        os.close(sender_audio)
        os.remove(audio_in_fifo_path)
        os.remove(audio_out_fifo_path)

if __name__ == "__main__":
    run_relay()
