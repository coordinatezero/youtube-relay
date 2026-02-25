#!/usr/bin/env python3
"""
YouTube RTMP relay using PyAV.
Maintains constant YouTube connection, switches between OBS input and black frames.
"""

import av
import numpy as np
import socket
import threading
import time
import logging
import signal
import sys
import subprocess
import json
from queue import Queue, Empty
from fractions import Fraction
import os.path
import pickle
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Configuration
with open('/etc/youtube-relay/config.json') as f:
    config = json.load(f)
    YOUTUBE_KEY = config['stream_key']
    YOUTUBE_URL = f"rtmp://a.rtmp.youtube.com/live2/{YOUTUBE_KEY}"

# Local RTMP server for OBS input
INPUT_URL = "rtmp://127.0.0.1:1935/live/obs"
YOUR_SERVER = "abunai.com"

# Video settings
WIDTH = 1920
HEIGHT = 1080
FPS = 30
VIDEO_BITRATE = "4500k"
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITRATE = "192k"

class OBSStreamMonitor:
    """Monitor incoming OBS RTMP stream"""
    
    def __init__(self, input_url):
        self.input_url = input_url
        self.is_alive = False
        self.container = None
        self.video_stream = None
        self.audio_stream = None
        self.should_run = True
        self.ffmpeg_process = None
        
    def check_stream(self):
        """Quickly check if RTMP stream is available"""
        try:
            p = subprocess.Popen([
                'ffprobe',
                '-v', 'error',
                '-timeout', '2000000',
                self.input_url
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p.wait(timeout=2)
            return p.returncode == 0
        except:
            return False
    
    def open_stream(self):
        """Open stream via ffmpeg pipe for actual streaming"""
        try:
            logging.info(f"Opening {self.input_url} for streaming...")
            
            self.ffmpeg_process = subprocess.Popen([
                'ffmpeg',
                '-loglevel', 'error',
                '-i', self.input_url,
                '-c', 'copy',
                '-f', 'mpegts',
                'pipe:1'
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10**8
            )
            
            # Give ffmpeg time to connect
            for i in range(20):
                time.sleep(0.5)
                if self.ffmpeg_process.poll() is not None:
                    stderr = self.ffmpeg_process.stderr.read().decode('utf-8', errors='ignore')
                    logging.error(f"ffmpeg died: {stderr}")
                    return False
                
                try:
                    self.container = av.open(self.ffmpeg_process.stdout, format='mpegts', timeout=1.0)
                    break
                except:
                    if i < 19:
                        continue
                    else:
                        logging.error("Timeout waiting for stream data")
                        self.ffmpeg_process.terminate()
                        return False
            
            # Find streams
            for stream in self.container.streams:
                if stream.type == 'video' and self.video_stream is None:
                    self.video_stream = stream
                elif stream.type == 'audio' and self.audio_stream is None:
                    self.audio_stream = stream
            
            if self.video_stream:
                logging.info("✓ Video stream opened for streaming!")
                self.is_alive = True
                return True
            else:
                logging.info("No video stream found")
                return False
                
        except Exception as e:
            logging.error(f"Failed to open stream: {e}")
            return False

#       #def check_stream(self):
#        """Quickly check if RTMP stream is available by reading a little data."""
#        try:
#            p = subprocess.Popen([
#                'ffmpeg',
#                '-loglevel', 'error',
#                '-rw_timeout', '2000000',     # 2s
#                '-i', self.input_url,
#                '-t', '2',
#                '-c', 'copy',
#                '-f', 'mpegts',
#                'pipe:1'
#            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
#            data = p.stdout.read(188 * 10)    # read ~10 TS packets
#            p.terminate()
#            return len(data) > 0
#        except Exception as e:
#            logging.info("check_stream failed: %r", e)
#            return False
    
#    def check_stream(self):
#        """Open stream directly via ffmpeg pipe"""
#        try:
#            #logging.info(f"Attempting to open {self.input_url} via ffmpeg pipe")
#            
#            # Use ffmpeg to pull and pipe
#            self.ffmpeg_process = subprocess.Popen([
#                'ffmpeg',
#                '-loglevel', 'error',
#                '-i', self.input_url,
#                '-c', 'copy',
#                '-f', 'mpegts',
#                'pipe:1'
#            ],
#            stdout=subprocess.PIPE,
#            stderr=subprocess.PIPE,
#            bufsize=10**8
#            )
#            
#            # Give ffmpeg time to connect (up to 10 seconds)
#            for i in range(20):
#                time.sleep(0.5)
#                if self.ffmpeg_process.poll() is not None:
#                    # Process died, read error
#                    stderr = self.ffmpeg_process.stderr.read().decode('utf-8', errors='ignore')
#                    logging.error(f"ffmpeg died: {stderr}")
#                    return False
#                
#                # Check if we have data
#                try:
#                    # Try to open container
#                    self.container = av.open(self.ffmpeg_process.stdout, format='mpegts', timeout=1.0)
#                    break
#                except:
#                    if i < 19:
#                        continue
#                    else:
#                        logging.error("Timeout waiting for stream data")
#                        self.ffmpeg_process.terminate()
#                        return False
#            
#            # Find streams
#            for stream in self.container.streams:
#                if stream.type == 'video' and self.video_stream is None:
#                    self.video_stream = stream
#                elif stream.type == 'audio' and self.audio_stream is None:
#                    self.audio_stream = stream
#            
#            if self.video_stream:
#                logging.info("✓ Video stream opened!")
#                self.is_alive = True
#                return True
#            else:
#                logging.info("No video stream found")
#                return False
#                
#        except Exception as e:
#            logging.error(f"Failed to open stream: {e}")
#            if self.ffmpeg_process:
#                try:
#                    self.ffmpeg_process.terminate()
#                except:
#                    pass
#            return False

    def close(self):
        """Close the stream"""
        if self.container:
            try:
                self.container.close()
            except Exception as e:
                logging.debug(f"Error closing container: {e}")
            finally:
                self.container = None
                self.video_stream = None
                self.audio_stream = None
        
        # CRITICAL FIX: Close the ffmpeg process
        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=2)
            except:
                try:
                    self.ffmpeg_process.kill()
                except:
                    pass
            self.ffmpeg_process = None
        
        self.is_alive = False

class BlackFrameGenerator:
    """Generate black video frames and silent audio"""
    
    def __init__(self, width, height, fps, sample_rate, channels):
        self.width = width
        self.height = height
        self.fps = fps
        self.sample_rate = sample_rate
        self.channels = channels
        self.color = 235
        
        # Audio samples per frame to maintain sync
        self.audio_samples_per_frame = int(sample_rate / fps)
        
    def generate_video_frame(self):
        """Generate a black video frame"""
        import numpy as np
        
        frame = av.VideoFrame(self.width, self.height, 'yuv420p')
        
        # Y plane (luma) - 16 for black, 235 for white
        #y_array = np.full((self.height, self.width), 16, dtype=np.uint8)
        y_array = np.full((self.height, self.width), self.color, dtype=np.uint8)
        self.color = self.color - 1 if self.color > 16 else 235
        frame.planes[0].update(y_array.tobytes())
        
        # U plane (chroma) - 128 for neutral
        u_array = np.full((self.height // 2, self.width // 2), 128, dtype=np.uint8)
        frame.planes[1].update(u_array.tobytes())
        
        # V plane (chroma) - 128 for neutral
        v_array = np.full((self.height // 2, self.width // 2), 128, dtype=np.uint8)
        frame.planes[2].update(v_array.tobytes())
        
        return frame
    
    def generate_audio_frame(self):
        """Generate silent audio frame"""
        # Use fltp format - AAC encoders prefer floating point
        frame = av.AudioFrame(
            format='fltp',
            layout='stereo',
            samples=self.audio_samples_per_frame
        )
        
        # Fill with silence (zeros)
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        
        frame.rate = self.sample_rate
        
        return frame

class YouTubeStreamer:
    """Stream to YouTube using PyAV"""
    
    def __init__(self, output_url, width, height, fps, video_bitrate, audio_bitrate):
        self.output_url = output_url
        self.width = width
        self.height = height
        self.fps = fps
        self.video_bitrate = video_bitrate
        self.audio_bitrate = audio_bitrate
        
        self.container = None
        self.video_stream = None
        self.audio_stream = None
        self.connected = False
        
        self.video_pts = 0
        self.audio_pts = 0
        self.logtime_a = time.time()
        self.logtime_v = time.time()
        
    def connect(self):
        """Connect to YouTube RTMP server and setup streams"""
        logging.info(f"Connecting to YouTube...")
        
        # If we are reconnecting, close the previous container cleanly
        if getattr(self, "container", None) is not None:
            try:
                self.container.close()
            except Exception as e:
                logging.info("PyAV container.close() failed during reconnect: %r", e)
            finally:
                self.container = None

        # Open output container
        self.container = av.open(
            self.output_url,
            mode='w',
            format='flv'
        )
            
        # Add video stream
        self.video_stream = self.container.add_stream('h264', rate=self.fps)
        self.video_stream.width = self.width
        self.video_stream.height = self.height
        self.video_stream.pix_fmt = 'yuv420p'
        self.video_stream.bit_rate = int(self.video_bitrate.replace('k', '000'))
        self.video_stream.options = {
            'preset': 'veryfast',
            'g': str(self.fps * 2),
            'profile': 'baseline',
            'level': '3.1',
            'tune': 'zerolatency',
        }
        #'bf': '0',
        #'flags': '+cgop'
            
        # Add audio stream - be very explicit
        self.audio_stream = self.container.add_stream('aac')
        self.audio_stream.rate = AUDIO_SAMPLE_RATE
        self.audio_stream.layout = 'stereo'
        self.audio_stream.bit_rate = int(self.audio_bitrate.replace('k', '000'))            

        self.connected = True
        self.video_pts = 0
        self.audio_pts = 0
            
        logging.info("✓ Connected to YouTube!")
        return True
            
    def send_video_frame(self, frame):
        """Send video frame to YouTube"""
        if not self.connected or not self.video_stream:
            logging.error(f"Cannot send video: connected={self.connected}, has_stream={self.video_stream is not None}")
            return False
        
        try:
            # Set PTS for the frame
            frame.pts = self.video_pts
            self.video_pts += 1
            
            # Encode and send
            packets = self.video_stream.encode(frame)
            total_bytes = 0
            for packet in packets:
                total_bytes += packet.size
                self.container.mux(packet)

            #if time.time() - self.logtime_v > 1:
                #logging.info(f"Sent video frame, pts={self.video_pts}, packets generated: {len(packets)}, total bytes: {total_bytes}")
                #self.logtime_v = time.time()
            
            return True
            
        except Exception as e:
            logging.error(f"Error sending video frame: {e}")
            return False
    
    def send_audio_frame(self, frame):
        """Send audio frame to YouTube"""
        if not self.connected or not self.audio_stream:
            logging.error(f"Cannot send audio: connected={self.connected}, has_stream={self.audio_stream is not None}")
            return False
        
        try:
            # Set PTS for the frame
            frame.pts = self.audio_pts
            self.audio_pts += frame.samples
            
            # Encode and send
            packets = self.audio_stream.encode(frame)

            total_bytes = 0
            for packet in packets:
                total_bytes += packet.size
                self.container.mux(packet)
            
            #if time.time() - self.logtime_a > 1:
                #logging.info(f"Sent audio frame, pts={self.audio_pts}, packets generated: {len(packets)}, total bytes: {total_bytes}")
                #self.logtime_a = time.time()
 
            return True
            
        except Exception as e:
            logging.error(f"Error sending audio frame: {e}")
            return False
    
    def flush(self):
        """Flush any remaining packets"""
        try:
            if self.video_stream:
                for packet in self.video_stream.encode():
                    self.container.mux(packet)
            
            if self.audio_stream:
                for packet in self.audio_stream.encode():
                    self.container.mux(packet)
        except:
            pass
    
    def disconnect(self):
        """Disconnect from YouTube"""
        if self.container:
            try:
                self.flush()
                self.container.close()
            except:
                pass
        
        self.container = None
        self.video_stream = None
        self.audio_stream = None
        self.connected = False

class YouTubeAPIHelper:
    """Helper class to interact with YouTube API for stream settings"""
    
    SCOPES = ['https://www.googleapis.com/auth/youtube']
    TOKEN_PATH = '/etc/youtube-relay/token.pickle'
    CLIENT_SECRET_PATH = '/etc/youtube-relay/client_secret.json'
    
    def __init__(self):
        self.service = None
        self.authenticate()
    
    def authenticate(self):
        """Authenticate with YouTube API using existing OAuth2 token"""
        creds = None

        if not os.path.exists(self.TOKEN_PATH):
            raise RuntimeError(
                "token.pickle not found. Run youtube-auth.py locally first."
            )

        # Load existing credentials
        with open(self.TOKEN_PATH, 'rb') as token:
            creds = pickle.load(token)

        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            logging.info("Refreshing YouTube API credentials...")
            creds.refresh(Request())
            # Save refreshed credentials
            with open(self.TOKEN_PATH, 'wb') as token:
                pickle.dump(creds, token)

        if not creds or not creds.valid:
            raise RuntimeError(
                "YouTube credentials invalid. Re-run youtube-auth.py locally."
            )

        self.service = build('youtube', 'v3', credentials=creds)
        logging.info("DEBUG: reached channels().list()")
        resp = self.service.channels().list(part='snippet', mine=True).execute()
        items = resp.get('items', [])
        logging.info("OAuth is acting as channel: %s", [
            {'id': c.get('id'), 'title': c.get('snippet', {}).get('title')}
            for c in items
        ])
        logging.info("✓ Authenticated with YouTube API")
    
    def get_active_broadcast(self):
        """Get the currently active live broadcast"""
        try:
            request = self.service.liveBroadcasts().list(
                part='id,snippet,status',
                mine=True,
                maxResults=10
            )
            response = request.execute()
            logging.info("Broadcast dump: %s", [
                {
                    'id': b.get('id'),
                    'title': b.get('snippet', {}).get('title'),
                    'lifeCycleStatus': b.get('status', {}).get('lifeCycleStatus'),
                    'privacyStatus': b.get('status', {}).get('privacyStatus'),
                }
                for b in response.get('items', [])
            ])            

            # 1) Prefer a live broadcast
            for item in response.get('items', []):
                if item.get('status', {}).get('lifeCycleStatus') == 'live':
                    return item

            # 2) Otherwise pick an upcoming one (and never pick a completed one)
            for item in response.get('items', []):
                st = item.get('status', {}).get('lifeCycleStatus')
                if st == 'complete':
                    continue
                if st in ('ready', 'testing', 'created'):
                    return item

            return None
            
        except HttpError as e:
            logging.error(f"Error getting broadcast: {e}")
            return None
    
    def set_broadcast_public(self, broadcast_id=None):
        """Set the broadcast to public"""
        try:
            if not broadcast_id:
                broadcast = self.get_active_broadcast()
                if not broadcast:
                    logging.warning("No active or upcoming broadcast found")
                    return False
                broadcast_id = broadcast['id']
            
            logging.info(f"Setting broadcast {broadcast_id} to public...")
            
            request = self.service.liveBroadcasts().update(
                part='status',
                body={
                    'id': broadcast_id,
                    'status': {
                        'privacyStatus': 'public'
                    }
                }
            )
            response = request.execute()
            
            logging.info("✓ Broadcast set to public")
            return True
            
        except HttpError as e:
            logging.error(f"Error updating broadcast: {e}")
            return False
    
    def update_broadcast_settings(self, title=None, description=None, 
                                   privacy='public', broadcast_id=None):
        """Update broadcast settings (title, description, privacy)"""
        try:
            if not broadcast_id:
                broadcast = self.get_active_broadcast()
                if not broadcast:
                    logging.warning("No active or upcoming broadcast found")
                    return False
                broadcast_id = broadcast['id']
            
            body = {'id': broadcast_id}
            parts = []
            
            if title or description:
                parts.append('snippet')
                body['snippet'] = {}
                if title:
                    body['snippet']['title'] = title
                if description:
                    body['snippet']['description'] = description
            
            if privacy:
                parts.append('status')
                body['status'] = {
                    'privacyStatus': privacy  # 'public', 'unlisted', or 'private'
                }
            
            if not parts:
                return True
            
            logging.info(f"Updating broadcast settings...")
            request = self.service.liveBroadcasts().update(
                part=','.join(parts),
                body=body
            )
            response = request.execute()
            
            logging.info(f"✓ Broadcast updated: privacy={privacy}")
            return True
            
        except HttpError as e:
            logging.error(f"Error updating broadcast: {e}")
            return False

class StreamRelay:
    """Main relay coordinator"""
    
    def __init__(self):
        self.youtube = YouTubeStreamer(
            YOUTUBE_URL, WIDTH, HEIGHT, FPS, VIDEO_BITRATE, AUDIO_BITRATE
        )
        self.obs_monitor = OBSStreamMonitor(INPUT_URL)
        self.obs_container = None
        self.black_gen = BlackFrameGenerator(
            WIDTH, HEIGHT, FPS, AUDIO_SAMPLE_RATE, AUDIO_CHANNELS
        )
        
        self.youtube_api = YouTubeAPIHelper()
        self.should_run = True
        self.current_mode = "black"  # "black" or "live"
        self.frame_count = 0
        
    def check_obs_status(self):
        """Check if OBS stream is available"""
        # Close existing connection if any
        if self.obs_monitor.container:
            self.obs_monitor.close()
            time.sleep(0.1)
        
        # Try to open stream
        logging.info("Checking for incoming stream...")
        result = self.obs_monitor.check_stream()
        logging.info(f"Stream check result: {result}")
        return result

def stream_from_obs(self):
        """Stream frames from OBS to YouTube"""
        try:
            # Decode frames from OBS and re-encode to YouTube
            for packet in self.obs_monitor.container.demux():
                if not self.should_run:
                    break
                
                frames = packet.decode()
                
                for frame in frames:
                    if isinstance(frame, av.VideoFrame):
                        # Always resize and reformat for YouTube
                        if frame.width != WIDTH or frame.height != HEIGHT:
                            frame = frame.reformat(width=WIDTH, height=HEIGHT)
                        
                        if frame.format.name != 'yuv420p':
                            frame = frame.reformat(format='yuv420p')

                        # Set time_base explicitly for encoder compatibility
                        frame.time_base = Fraction(1, FPS)
                        
                        # Send frame
                        result = self.youtube.send_video_frame(frame)
                        if not result:
                            logging.error("stream_from_obs: Failed to send video frame")
                            return False
                        
                        self.frame_count += 1
                        
                    elif isinstance(frame, av.AudioFrame):
                        # Always resample
                        if not hasattr(self, 'resampler'):
                            self.resampler = av.AudioResampler(
                                format='fltp',
                                layout='stereo',
                                rate=AUDIO_SAMPLE_RATE
                            )
                        
                        resampled_frames = self.resampler.resample(frame)
                        for resampled_frame in resampled_frames:
                            if not self.youtube.send_audio_frame(resampled_frame):
                                logging.error("stream_from_obs: Failed to send audio frame")
                                return False

            return False
            
        except Exception as e:
            logging.error(f"Error streaming from OBS: {e}")
            return False
#    def stream_from_obs(self):
#        """Stream frames from OBS to YouTube"""
#        try:
#            # Don't do custom rate limiting - just pass frames through
#            # PyAV and ffmpeg handle timing
#            
#            # Decode frames from OBS and re-encode to YouTube
#            for packet in self.obs_monitor.container.demux():
#                if not self.should_run:
#                    break
#                
#                frames = packet.decode()
#                
#                for frame in frames:
#                    if isinstance(frame, av.VideoFrame):
#                        # Always resize and reformat for YouTube
#                        if frame.width != WIDTH or frame.height != HEIGHT:
#                            frame = frame.reformat(width=WIDTH, height=HEIGHT)
#                        
#                        if frame.format.name != 'yuv420p':
#                            frame = frame.reformat(format='yuv420p')
#
#                        # Set time_base explicitly for encoder compatibility
#                        frame.time_base = Fraction(1, FPS)
#                        
#                        # Send frame - don't mess with PTS, let encoder handle it
#                        #logging.info("About to send video frame directly")
#                        result = self.youtube.send_video_frame(frame)
#                        #logging.info(f"External send video result: {result}")
#                        if not result:
#                            logging.error("stream_from_obs: Failed to send video frame")
#                            return False
#                        
#                        self.frame_count += 1
#                        
#                    elif isinstance(frame, av.AudioFrame):
#                        # Check if resampling is needed
#                        #needs_resample = (frame.rate != 48000 or 
#                        #                  frame.layout.name != 'stereo' or 
#                        #                  frame.format.name != 'fltp')
#                        needs_resample = True
#                        
#                        if needs_resample:
#                            # Always resample audio to YouTube standards
#                            if not hasattr(self, 'resampler'):
#                                self.resampler = av.AudioResampler(
#                                    format='fltp',
#                                    layout='stereo',
#                                    rate=AUDIO_SAMPLE_RATE
#                                )
#                            
#                            resampled_frames = self.resampler.resample(frame)
#                            #logging.info(f"Resampler returned {len(resampled_frames)} frames")
#                            for resampled_frame in resampled_frames:
#                                if not self.youtube.send_audio_frame(resampled_frame):
#                                    logging.error("stream_from_obs: Failed to send audio frame")
#                                    return False
#                        else:
#                            #logging.info("About to send audio frame directly")
#                            # No resampling needed, send directly
#                            result = self.youtube.send_audio_frame(frame)
#                            #logging.info(f"External send audio result: {result}")
#                            if not result:
#                                logging.error("Failed to send audio frame")
#                                return False
#
#            return False
#            
#        except Exception as e:
#            logging.error(f"Error streaming from OBS: {e}")
#            return False

    def stream_black_frames(self):
        """Stream black frames to YouTube"""
        # Clear any existing resampler from previous stream
        if hasattr(self, 'resampler'):
            del self.resampler
        
        frame_duration = 1.0 / FPS
        start_time = time.time()
        frame_num = 0
        
        logging.info("⚫ Streaming black frames...")
        
        while self.should_run and self.current_mode == "black":
            try:
                # Calculate target time for this frame
                target_time = start_time + (frame_num * frame_duration)
                
                # Generate and send frames
                video_frame = self.black_gen.generate_video_frame()
                if not self.youtube.send_video_frame(video_frame):
                    logging.error("Failed to send black video frame")
                    return False
                
                audio_frame = self.black_gen.generate_audio_frame()
                if not self.youtube.send_audio_frame(audio_frame):
                    logging.error("Failed to send silent audio frame")
                    return False
                
                frame_num += 1
                self.frame_count += 1
                
                # Log periodically
                if frame_num % (FPS * 30) == 0:  # Every 30 seconds
                    logging.info(f"Streaming black frames ({frame_num} sent)")
                
                # Sleep until next frame time
                now = time.time()
                sleep_time = target_time - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
                # Check for OBS every 3 seconds
                if frame_num % (FPS * 3) == 0:
                    if self.check_obs_status():
                        logging.info("🟢 external stream detected!")
                        self.current_mode = "live"
                        return True
            
            except Exception as e:
                logging.error(f"Error streaming black frames: {e}")
                return False
        
        return True

    def run(self):
        """Main run loop"""
        logging.info("="*60)
        logging.info("YouTube RTMP Relay (PyAV)")
        logging.info("="*60)
        logging.info(f"Configure external to stream to: rtmp://{YOUR_SERVER}:1935/live/obs")
        logging.info("="*60)
        
        self.should_run = True
        logging.info(f"In YouTubeStreamer.run")
        
        while self.should_run:
            # Connect to YouTube
            if not self.youtube.connect():
                logging.error("Failed to connect to YouTube, retrying in 10s...")
                time.sleep(10)
                continue

            try:
                self.youtube_api.set_broadcast_public()
            except Exception as e:
                logging.warning(f"Could not set broadcast public: {e}")

            try:
                # Check if external stream exists
                logging.info(f"checking if external stream is there")
                has_external = self.check_obs_status()
                
                if has_external:
                    # Reconnect BEFORE switching modes
                    logging.info("🟢 External stream detected, reconnecting encoder...")
                    self.obs_monitor.close()  # Close the check connection first
                    self.youtube.disconnect()
                    time.sleep(1)
                    if not self.youtube.connect():
                        logging.error("Failed to reconnect")
                        continue
                    
                    # Now reopen external stream with fresh encoder
                    if not self.obs_monitor.open_stream():
                        logging.error("External stream disappeared")
                        continue
                    
                    logging.info("🟢 Streaming from external")
                    self.current_mode = "live"
                    
                    # Stream from OBS
                    success = self.stream_from_obs()
                    
                    # OBS disconnected
                    self.obs_monitor.close()
                    logging.warning("External stream ended, switching to black frames")
                    self.current_mode = "black"
                    
                    # Don't reconnect - just continue with same connection
                    
                else:
                    # No OBS stream, send black frames
                    self.current_mode = "black"
                    success = self.stream_black_frames()
                    
                    if not success and self.should_run:
                        logging.error("Black frame streaming failed, reconnecting...")
                        self.youtube.disconnect()
                        time.sleep(5)
                
            except KeyboardInterrupt:
                logging.info("\nShutdown requested...")
                break
            except Exception as e:
                logging.error(f"Unexpected error: {e}")
                self.youtube.disconnect()
                time.sleep(5)
        
        logging.info("Cleaning up...")
        self.youtube.disconnect()
        self.obs_monitor.close()
        logging.info("Stopped.")


def signal_handler(sig, frame):
    """Handle shutdown signals"""
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    relay = StreamRelay()
    relay.run()
