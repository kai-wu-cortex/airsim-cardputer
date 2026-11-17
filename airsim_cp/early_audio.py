"""MU300's existing authenticated receive-only early-media lease protocol."""
import base64
import socket
import subprocess
import threading
import time
import secrets
from .audio import AudioSession
from .protocol import AirSIMError, b64, canonical, mac
from .playback import SpeechPlayback, playback_command

EARLY_STATES = {'dialing', 'alerting', 'connecting', 'ringback', 'early_media'}


class EarlyAudio(AudioSession):
    early = True

    def start(self, client, call_id):
        health = client.health()
        calls, _ = client.signed('GET', '/api/calls/status')
        call = calls.get('active') or {}
        if (health.get('agent_kind') != 'mu300' or call.get('id') != call_id
                or call.get('direction') != 'outgoing' or call.get('state') not in EARLY_STATES):
            return
        with self.lock:
            if self.closed or self.stop_event.is_set():
                return
            self.active = True
            worker = threading.Thread(target=self.receive, args=(client, call_id), daemon=True)
            self.threads = [worker]
            worker.start()

    @staticmethod
    def preface(client, grant):
        binding = '\n'.join([grant['agent_id'], grant['call_id'], grant['session_id'],
                             str(grant['expires_at']), 'recvonly', 's16le-8000-mono']).encode()
        stamp = int(time.time()+client.profile.get('clock_offset', 0))
        nonce = b64(secrets.token_bytes(16))
        signature = mac(client.secret, canonical('PCM', '/v1/pcm/early-media', binding, stamp, nonce))
        return f'AIRSIMEM1 {stamp} {nonce} {signature} {base64.b64encode(binding).decode()}\n'.encode()

    def receive(self, client, call_id):
        try:
            deadline = time.monotonic()+180
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                try:
                    grant = client.early_grant(call_id)
                    break
                except AirSIMError as error:
                    # No EARLYMEDIA indication yet is normal, not a call failure.
                    if error.status != 202:
                        return
                    if self.stop_event.wait(.6):return
            else:return
            if self.closed or self.stop_event.is_set():return
            connection = socket.create_connection((client.profile['host'], client.profile['pcm_port']), timeout=8)
            with self.lock:
                if self.closed or self.stop_event.is_set():
                    connection.close();return
                self.socket = connection
            connection.sendall(self.preface(client, grant))
            connection.settimeout(20)
            if self.exact(connection, 11) != b'AIRSIMREADY':return
            connection.settimeout(8)
            with self.lock:
                if self.closed or self.stop_event.is_set():return
                playback = subprocess.Popen(playback_command(), stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.processes.append(playback)
                renewal = threading.Thread(target=self.renew, args=(client, call_id, grant['session_id']), daemon=True)
                self.threads.append(renewal);renewal.start()
            speech = SpeechPlayback()
            while not self.stop_event.is_set():
                frame = self.exact(connection, 320)
                playback.stdin.write(speech.process(frame));playback.stdin.flush()
                self.downlink_bytes += len(frame)
        except (AirSIMError, OSError):
            pass  # Connected/ended/lease-expired stops early media normally.
        finally:
            self.stop()

    def renew(self, client, call_id, session):
        while not self.stop_event.wait(10):
            try:
                if client.early_grant(call_id)['session_id'] != session:break
            except (AirSIMError, OSError):break
        self.stop()
