"""Explicit, foreground-only VoWLAN PCM: authenticated TCP, 8 kHz mono S16LE."""
import socket
import subprocess
import threading
import time
import secrets

from .protocol import AirSIMError, b64, canonical, mac
from .playback import SpeechPlayback, playback_command
from .echo import EchoRoute


class AudioSession:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.socket = None
        self.processes = []
        self.threads = []
        self.active = False
        self.closed = False
        self.error = ''
        self.route = None
        self.muted = False
        self.uplink_bytes = self.downlink_bytes = 0

    @staticmethod
    def exact(stream, size):
        result = bytearray()
        while len(result) < size:
            data = stream.recv(size - len(result))
            if not data:
                raise AirSIMError('手机关闭了语音连接')
            result.extend(data)
        return bytes(result)

    def start(self, client, call_id):
        # Never attach automatically to an unrelated or subsequently replaced call.
        client.health()
        calls, _ = client.signed('GET', '/api/calls/status')
        current = calls.get('active') or {}
        if current.get('id') != call_id or current.get('state') not in ('active', 'connected'):
            raise AirSIMError('请在电话接通后开启语音')
        with self.lock:
            if self.closed:
                raise AirSIMError('应用已退出')
            if self.active or self.threads:
                raise AirSIMError('语音已经启动')
            self.stop_event.clear()
            self.error = ''
            self.uplink_bytes = self.downlink_bytes = 0
        try:
            with self.lock:
                if self.closed or self.stop_event.is_set():
                    raise AirSIMError('语音已取消')
                self.route = EchoRoute()
            self.route.start(self.stop_event)
            profile = client.profile
            connection = socket.create_connection((profile['host'], profile['pcm_port']), timeout=8)
            with self.lock:
                if self.closed:
                    connection.close()
                    raise AirSIMError('应用已退出')
                self.socket = connection
            nonce = b64(secrets.token_bytes(16))
            timestamp = int(time.time() + profile.get('clock_offset', 0))
            signature = mac(client.secret, canonical('PCM', '/v1/pcm', b'', timestamp, nonce))
            connection.settimeout(25)  # MU300 may first finish receive-only DSP teardown.
            connection.sendall(f'AIRSIMVWL1 {timestamp} {nonce} {signature}\n'.encode())
            if self.exact(connection, 11) != b'AIRSIMREADY':
                raise AirSIMError('手机语音握手失败')
            connection.settimeout(8)
            if self.stop_event.is_set():
                raise AirSIMError('语音已取消')
            with self.lock:
                if self.closed or self.stop_event.is_set():
                    raise AirSIMError('语音已取消')
                playback = subprocess.Popen(self.route.playback(), stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.processes.append(playback)
                capture = subprocess.Popen(self.route.capture(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.processes.append(capture)
                self.active = True
            speech = SpeechPlayback()
            def downlink():
                while not self.stop_event.is_set():
                    frame = self.exact(connection, 320)
                    playback.stdin.write(speech.process(frame)); playback.stdin.flush()
                    self.downlink_bytes += len(frame)
            microphone = SpeechPlayback(gain_db=9)
            def uplink():
                while not self.stop_event.is_set():
                    frame = capture.stdout.read(320)
                    if len(frame) != 320:
                        raise AirSIMError('麦克风不可用')
                    frame=bytes(len(frame)) if self.muted else microphone.process(frame)
                    connection.sendall(frame)
                    self.uplink_bytes += len(frame)
            def run(function):
                try:
                    function()
                except Exception:
                    if not self.stop_event.is_set():
                        self.error = '语音连接中断，请检查手机通话'
                finally:
                    self.stop()
            with self.lock:
                if self.closed or self.stop_event.is_set():
                    raise AirSIMError('语音已取消')
                self.threads = [threading.Thread(target=run, args=(function,), daemon=True)
                                for function in (downlink, uplink)]
                for thread in self.threads:
                    thread.start()
        except Exception:
            self.stop()
            raise AirSIMError('语音启动失败，请检查手机和音频设备') from None

    def stop(self):
        with self.lock:
            self.stop_event.set()
            self.active = False
            if self.socket:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.socket.close(); self.socket = None
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=0.5)
            self.processes = []
            if self.route:
                self.route.close(); self.route = None
            threads, self.threads = self.threads, []
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)

    def close(self):
        with self.lock:
            self.closed = True
        self.stop()
