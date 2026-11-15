"""Local-only AirSIM protocol fixture. This is not a real phone/Telecom test."""
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encode(value): return base64.urlsafe_b64encode(value).decode().rstrip('=')
def decode(value): return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
def sign(secret, value): return encode(hmac.new(secret, value.encode(), hashlib.sha256).digest())


class Phone:
    def __init__(self):
        self.secret = secrets.token_bytes(32)
        self.private = X25519PrivateKey.generate()
        self.session = secrets.token_hex(12)
        self.agent = 'cardputer-test-fixture'
        self.code = '123456'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.handler())
        self.port = self.server.server_address[1]
        self.pcm_port = self.port
        self.clock_offset = 120
        self.tamper_route = self.tamper_health = self.tamper_dial = False
        self.expired = False
        self.direct_gateway = False
        self.last_registration = None
        self.fail_post = False
        self.seen_nonces = set()
        self.requests = []
        self.active = None
        self.messages = [{'sender': 'Demo phone', 'content': '你好，Cardputer！HTTP、中文显示和短信阅读测试。此消息来自本机模拟服务。', 'timestamp': '2026-11-14T12:00:00Z', 'delivery_id': 'fixture-1'}]

    def profile(self):
        return {'host': '127.0.0.1', 'control_port': self.port, 'pcm_port': self.pcm_port,
                'agent_id': self.agent, 'secret': encode(self.secret), 'clock_offset': self.clock_offset,
                'test_fixture': True}

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        return self

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def handler(self):
        phone = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def respond(self, code, value):
                encoded = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
            def do_GET(self): self.handle_api()
            def do_POST(self): self.handle_api()
            def handle_api(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                data = json.loads(body) if body else {}
                phone.requests.append((self.command, self.path, data))
                if self.path == '/pair/v1/info':
                    public = phone.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                    now = (time.time() + phone.clock_offset) * 1000
                    return self.respond(200, {'product': 'airsim-pair', 'version': 1, 'session_id': phone.session,
                        'server_public_key': encode(public), 'server_time': now, 'expires_at': now + (-1000 if phone.expired else 120000)})
                if self.path == '/pair/v1/claim':
                    try:
                        shared = phone.private.exchange(X25519PublicKey.from_public_bytes(decode(data['client_public_key'])))
                        prk = hmac.new(phone.session.encode(), shared, hashlib.sha256).digest()
                        key = hmac.new(prk, b'AirSIMPair/v1:' + phone.code.encode() + b'\x01', hashlib.sha256).digest()
                        sealed = decode(data['sealed_registration'])
                        registration = json.loads(AESGCM(key).decrypt(sealed[:12], sealed[12:], phone.session.encode()))
                        if registration.get('local_mode'):
                            assert registration['cloud_enabled'] is False
                        else:
                            assert registration['cloud_enabled'] is True
                            assert all(registration.get(k) for k in ('device_id','device_secret','relay_url','bundle_id','environment'))
                            assert any(registration.get(k) for k in ('alert_token','voip_token','watch_voip_token'))
                        phone.last_registration=registration
                        phone.secret = decode(registration['vowlan_secret'])
                    except Exception:
                        return self.respond(401, {})
                    proof = sign(phone.secret, f"AIRSIM-DIRECT1\n{phone.session}\n{data['client_public_key']}\n{phone.agent}\n{phone.port}\n{phone.pcm_port}")
                    return self.respond(200, {'agent_id': phone.agent, 'control_port': phone.port, 'pcm_port': phone.pcm_port,
                                             'route_proof': 'invalid' if phone.tamper_route else proof})
                nonce = self.headers.get('X-AirSIM-VoWLAN-Nonce', '')
                stamp = self.headers.get('X-AirSIM-VoWLAN-Timestamp', '0')
                canonical = '\n'.join([self.command, self.path, hashlib.sha256(body).hexdigest(), stamp, nonce])
                if (self.headers.get('X-AirSIM-VoWLAN-Version') != '1' or nonce in phone.seen_nonces
                    or abs(int(stamp) - time.time() - phone.clock_offset) > 30
                    or not hmac.compare_digest(sign(phone.secret, canonical), self.headers.get('X-AirSIM-VoWLAN-Signature', ''))):
                    return self.respond(401, {})
                phone.seen_nonces.add(nonce)
                if phone.fail_post and self.command == 'POST': return self.respond(503, {})
                if self.path == '/v1/health':
                    return self.respond(200, {**({'transport':'vowlan','battery':{'percent':38},'phone_number':'10000'} if phone.direct_gateway else {}), 'ok': True, 'agent_id': phone.agent,
                        'identity_proof': 'invalid' if phone.tamper_health else sign(phone.secret, f'AIRSIM-HEALTH1\n{nonce}\n{phone.agent}')})
                if self.path == '/api/android/status': return self.respond(404,{}) if phone.direct_gateway else self.respond(200, {'model': 'HTTP 模拟手机'})
                if self.path == '/api/sms': return self.respond(200, phone.messages)
                if self.path == '/api/calls/status': return self.respond(200, {'active': phone.active, 'history': []})
                if self.path == '/api/sms/send':
                    phone.messages.append({'sender': data['phone'], 'content': data['message'], 'direction': 'outgoing', 'delivery_id': secrets.token_hex(8)})
                    return self.respond(200, {'sent': True, 'segments': 1})
                if self.path == '/api/calls/dial':
                    phone.active = {'id': 'fixture-call', 'state': 'active', 'number': data['number']}
                    proof = sign(phone.secret, f"AIRSIM-DIAL1\n{nonce}\n{data['operation_id']}\nfixture-call\nfixture-backend")
                    return self.respond(200, {'operation_id': data['operation_id'], 'android_id': 'fixture-call',
                        'backend_id': 'fixture-backend', 'operation_proof': 'invalid' if phone.tamper_dial else proof})
                if self.path in ('/api/calls/answer', '/api/calls/hangup', '/api/calls/reject'):
                    if not phone.active or data.get('call_id') != phone.active['id']: return self.respond(409, {})
                    if self.path.endswith('answer'): phone.active['state'] = 'active'
                    else: phone.active = None
                    return self.respond(200, {'ok': True})
                self.respond(404, {})
        return Handler


if __name__ == '__main__':
    import argparse
    import signal
    parser = argparse.ArgumentParser(); parser.add_argument('config', type=Path); args = parser.parse_args()
    from airsim_cp.protocol import ProfileStore
    phone = Phone().start(); ProfileStore(args.config).save(phone.profile())
    print('Loopback-only test phone ready', flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait(); phone.close()
