"""Real ALSA/PipeWire I/O through a loopback authenticated PCM peer; no actual telephone call."""
import json
from pathlib import Path
import socket
import threading
import time

from airsim_cp.audio import AudioSession
from airsim_cp.protocol import Client
from mock_phone import Phone, sign
import hashlib

phone = Phone().start()
phone.active = {'id': 'fixture-call', 'state': 'active'}
server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1); server.settimeout(10)
phone.pcm_port = server.getsockname()[1]
stop = threading.Event(); observed = {'authenticated': False, 'received_bytes': 0, 'sent_bytes': 0}

def peer():
    connection = None
    try:
        connection, _ = server.accept(); connection.settimeout(4)
        line = bytearray()
        while not line.endswith(b'\n') and len(line) < 1024:
            part = connection.recv(1)
            if not part: raise RuntimeError('missing handshake')
            line.extend(part)
        magic, stamp, nonce, signature = line.decode().strip().split(' ')
        expected = sign(phone.secret, '\n'.join(['PCM', '/v1/pcm', hashlib.sha256(b'').hexdigest(), stamp, nonce]))
        assert magic == 'AIRSIMVWL1' and signature == expected
        observed['authenticated'] = True
        connection.sendall(b'AIR'); connection.sendall(b'SIMREADY')
        def receive():
            try:
                while not stop.is_set():
                    data = connection.recv(4096)
                    if not data: break
                    observed['received_bytes'] += len(data)
            except OSError: pass
        reader = threading.Thread(target=receive, daemon=True); reader.start()
        while not stop.is_set():
            connection.sendall(bytes(320)); observed['sent_bytes'] += 320; stop.wait(0.02)
        reader.join(timeout=0.5)
    except Exception as error:
        if not stop.is_set(): observed['error'] = type(error).__name__
    finally:
        if connection: connection.close()

thread = threading.Thread(target=peer, daemon=True); thread.start()
audio = AudioSession()
try:
    audio.start(Client(phone.profile()), 'fixture-call')
    time.sleep(2)
    observed.update({'client_uplink_bytes': audio.uplink_bytes, 'client_downlink_bytes': audio.downlink_bytes,
                     'active': audio.active, 'error': audio.error, 'real_phone_call': False})
    assert observed['authenticated'] and audio.uplink_bytes >= 8000 and audio.downlink_bytes >= 8000 and audio.active
finally:
    stop.set(); audio.close(); thread.join(timeout=2); server.close(); phone.close()
    Path('/home/pi/.cache/airsim-cardputer-test/audio-check.json').write_text(json.dumps(observed, indent=2))
print(json.dumps(observed))
