"""AirSIM VoBLE v1 wire format; independent implementation of the existing protocol."""
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
from collections import OrderedDict
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from .protocol import AirSIMError, b64, unb64

PREFIX = b'AirSIM/VoBLE/v1/'
SERVICE = '7e57a000-7a17-4d9f-9a3c-41a17b1e0001'
CAPABILITY, PAIRING, CONTROL_RX, CONTROL_TX, AUDIO_UP, AUDIO_DOWN = [
    f'7e57a00{i}-7a17-4d9f-9a3c-41a17b1e0001' for i in range(1, 7)]


def hkdf(source, salt, label):
    return HKDF(hashes.SHA256(), 32, salt, PREFIX + label).derive(source)


def tag(key, value):
    return hmac.new(key, value, hashlib.sha256).digest()[:16]


def lp(value):
    return struct.pack('>H', len(value)) + value


PAIRING_ERRORS = {
    'peer is already paired; authenticated reconnect required': '手机已有蓝牙配对，本机没有对应密钥。请使用原已保存连接；新设备需要在手机处理旧配对，会影响原客户端。',
    'another central owns VoBLE': '手机蓝牙正被另一客户端占用，请先断开另一客户端。',
    'pairing challenge expired': '六位配对码已过期，请重新扫描并核对新码。',
    'unknown pairing challenge': '本次配对已失效，请重新扫描。',
    'pairing confirmation failed': '双方六位配对码不一致，请重新核对。',
    'peer is not paired': '手机已无原蓝牙配对记录，请重新配对。',
    'reconnect authentication failed': '保存的蓝牙密钥与手机不一致，请检查原配对记录。',
}


def check_pairing_response(response):
    if response.get('ok') is False or response.get('error'):
        reason=response.get('error')
        # Only allowlisted protocol errors go to logs; never print peer payloads or keys.
        known=reason if isinstance(reason,str) and reason in PAIRING_ERRORS else 'unknown'
        logging.getLogger(__name__).warning('VoBLE pairing rejected: %s',known)
        raise AirSIMError(PAIRING_ERRORS.get(known,'手机拒绝蓝牙配对，请检查手机 AirSIM 的配对状态。'))


def recovery_matches(challenge,key):
    try:
        transcript=b''.join(lp(item) for item in (b'\1',challenge['android_identity'].encode(),
            unb64(challenge['android_public']),bytes(32),unb64(challenge['android_nonce']),bytes(32)))
        expected=tag(unb64(key),PREFIX+b'address-recovery/'+challenge['challenge_id'].encode()+transcript)
        return hmac.compare_digest(expected,unb64(challenge['address_recovery_proof']))
    except (KeyError,ValueError,TypeError):return False


class Pairing:
    def __init__(self, challenge, stored=None):
        check_pairing_response(challenge)
        if challenge.get('op') != 'begin': raise AirSIMError('蓝牙配对响应无效')
        self.challenge = challenge['challenge_id']
        self.identity = challenge['android_identity']
        self.expires = time.monotonic() + 110
        public, nonce = unb64(challenge['android_public']), unb64(challenge['android_nonce'])
        if len(public) != 32 or len(nonce) != 32 or not self.identity:
            raise AirSIMError('蓝牙配对材料无效')
        private = X25519PrivateKey.generate()
        self.public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.nonce = secrets.token_bytes(32)
        transcript = b''.join(lp(item) for item in (b'\1', self.identity.encode(), public, self.public, nonce, self.nonce))
        self.key = hkdf(private.exchange(X25519PublicKey.from_public_bytes(public)), hashlib.sha256(transcript).digest(), b'pairing')
        self.sas = f"{int.from_bytes(hmac.new(self.key, PREFIX + b'sas' + transcript, hashlib.sha256).digest()[:4], 'big') % 1000000:06d}"
        self.reconnect = None
        if stored:
            if stored['agent_id'] != self.identity: raise AirSIMError('蓝牙设备身份发生变化')
            if challenge.get('address_recovery_proof'):
                if not recovery_matches(challenge,stored['ble_key']):
                    raise AirSIMError('手机蓝牙密钥已变化，无法认证重连；请重新核对配对记录')
            self.reconnect = tag(unb64(stored['ble_key']), PREFIX + b'reconnect' + transcript)

    def message(self, confirm=False):
        if time.monotonic() > self.expires: raise AirSIMError('蓝牙配对已超时')
        value = {'op': 'confirm' if confirm else 'prepare', 'challenge_id': self.challenge,
                 'iphone_public': b64(self.public), 'iphone_nonce': b64(self.nonce)}
        if confirm:
            value.update({'reconnect_tag': b64(self.reconnect)} if self.reconnect else {'sas': self.sas})
        return value

    def verify_ready(self, response):
        check_pairing_response(response)
        if response.get('ready') is not True or not hmac.compare_digest(
                tag(self.key, PREFIX + b'ready' + self.challenge.encode()), unb64(response.get('confirmation_tag', ''))):
            raise AirSIMError('蓝牙配对确认签名无效')


def session_key(pairing_key, session, call_id, generation, label=b'control'):
    context = lp(session) + lp(call_id.encode()) + struct.pack('>Q', generation)
    return hkdf(pairing_key, hashlib.sha256(context).digest(), label)


def control_encode(key, session, command, call_id, generation, body):
    nonce = secrets.token_bytes(12)
    body = dict(body, timestamp_ms=int(time.time() * 1000))
    payload = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    cmd, call = command.encode(), call_id.encode()
    if max(len(session), len(nonce), len(cmd), len(call)) > 255 or len(payload) > 3500:
        raise AirSIMError('蓝牙消息过长')
    prefix = struct.pack('>4sQBBBBH', b'\xc3\x7e\x01\x02', generation, len(session), len(nonce), len(cmd), len(call), len(payload)) + session + nonce + cmd + call + payload
    auth = lp(nonce) + struct.pack('>Q', body['timestamp_ms']) + prefix
    return prefix + tag(session_key(key, session, call_id, generation), auth)


class ControlReader:
    def __init__(self): self.seen = OrderedDict()
    def decode(self, key, data):
        if len(data) < 34: raise AirSIMError('蓝牙消息截断')
        magic, generation, ns, nn, nc, ni, np = struct.unpack('>4sQBBBBH', data[:18])
        if magic != b'\xc3\x7e\x01\x02' or len(data) != 34 + ns + nn + nc + ni + np:
            raise AirSIMError('蓝牙消息格式无效')
        offset = 18
        values=[]
        for length in (ns, nn, nc, ni, np):
            values.append(data[offset:offset+length]); offset += length
        session, nonce, cmd, call, payload = values
        call_id = call.decode(); body = json.loads(payload)
        stamp = body['timestamp_ms']
        auth = lp(nonce) + struct.pack('>Q', stamp) + data[:-16]
        if not hmac.compare_digest(tag(session_key(key, session, call_id, generation), auth), data[-16:]):
            raise AirSIMError('蓝牙消息认证失败')
        if not nonce or nonce in self.seen or not -5 <= time.time() - stamp / 1000 <= 30:
            raise AirSIMError('蓝牙消息重放或设备时钟不一致')
        self.seen[nonce] = True
        if len(self.seen) > 256: self.seen.popitem(last=False)
        return {'command': cmd.decode(), 'call_id': call_id, 'generation': generation, 'session': session, 'body': body}


def fragments(payload, kind, message_id, mtu=180):
    capacity = mtu - 8
    count = max(1, (len(payload) + capacity - 1) // capacity) if capacity > 0 else 65
    if len(payload) > 4096 or count > 64 or kind not in (1, 2): raise AirSIMError('蓝牙消息超过 MTU 限制')
    return [struct.pack('>4sHBB', bytes([0xd4, 0x6b, 1, kind]), message_id & 65535, i, count)
            + payload[i*capacity:(i+1)*capacity] for i in range(count)]


class Reassembler:
    def __init__(self): self.pending = {}
    def append(self, data):
        now = time.monotonic()
        self.pending = {k:v for k,v in self.pending.items() if now-v[0] < 2}
        if len(data) < 8 or data[:3] != b'\xd4\x6b\x01' or data[3] not in (1,2): raise AirSIMError('无效蓝牙分片')
        message, index, count = struct.unpack('>HBB', data[4:8])
        if not 0 <= index < count <= 64: raise AirSIMError('无效蓝牙分片序号')
        key = (data[3], message)
        first, expected, parts = self.pending.setdefault(key, (now,count,{}))
        if expected != count or len(self.pending)>32:
            self.pending.clear(); raise AirSIMError('蓝牙分片不一致')
        parts[index] = data[8:]
        if sum(map(len, parts.values())) > 4096:
            self.pending.pop(key); raise AirSIMError('蓝牙消息过大')
        if len(parts) == count:
            self.pending.pop(key); return data[3], b''.join(parts[i] for i in range(count))
        return None


def mulaw_encode(pcm):
    if len(pcm) % 2: raise AirSIMError('PCM 长度无效')
    output=bytearray()
    for (sample,) in struct.iter_unpack('<h', pcm):
        sign = (sample >> 8) & 128
        magnitude = min(32635, abs(sample)) + 132
        exponent=7; mask=0x4000
        while exponent>0 and magnitude & mask == 0: exponent-=1; mask>>=1
        output.append((~(sign | exponent << 4 | ((magnitude >> (exponent+3)) & 15))) & 255)
    return bytes(output)


def mulaw_decode(data):
    result=bytearray()
    for value in data:
        value = (~value) & 255
        magnitude = ((((value & 15) << 3) + 132) << ((value >> 4) & 7)) - 132
        result.extend(struct.pack('<h', -magnitude if value & 128 else magnitude))
    return bytes(result)


def media_crypt(key, direction, stream, sequence, timestamp, data, decrypt=False):
    nonce = struct.pack('>BBHII', direction, stream, 0, timestamp, sequence)
    aad = struct.pack('>BBBII', direction, 1, stream, sequence, timestamp)
    return (AESGCM(key).decrypt if decrypt else AESGCM(key).encrypt)(nonce, data, aad)


def audio_fragments(key, sequence, timestamp, pcm, mtu=180):
    sealed = media_crypt(key, 1, 1, sequence, timestamp, mulaw_encode(pcm))
    cap=min(255,mtu-18)
    if cap < 1: raise AirSIMError('蓝牙音频 MTU 不足')
    count=(len(sealed)+cap-1)//cap
    if count>64: raise AirSIMError('蓝牙音频分片过多')
    return [struct.pack('>7sIIBBB', b'\xa5\x5a\x01\x01\x03\x01\x01', sequence,timestamp,i,count,len(sealed[i*cap:(i+1)*cap]))+sealed[i*cap:(i+1)*cap] for i in range(count)]


class AudioReader:
    def __init__(self): self.pending={}; self.seen=OrderedDict()
    def append(self, key, notification):
        frames=[]; offset=0; parsed=[]
        while offset<len(notification):
            if len(notification)-offset<18: raise AirSIMError('蓝牙音频截断')
            data=notification[offset:]; magic,seq,stamp,index,count,size=struct.unpack('>7sIIBBB',data[:18])
            if magic[:4]!=b'\xa5\x5a\x01\x01' or magic[5]!=1 or not 0<=index<count<=64 or len(data)<18+size:
                raise AirSIMError('蓝牙音频分片无效')
            parsed.append((magic,seq,stamp,index,count,data[18:18+size]));offset+=18+size
        now=time.monotonic();self.pending={k:v for k,v in self.pending.items() if now-v[0]<0.2}
        for magic,seq,stamp,index,count,data in parsed:
            ident=(magic[6],seq,stamp)
            if ident in self.seen: continue
            first,expected,parts=self.pending.setdefault(ident,(now,count,{}))
            if count!=expected or len(self.pending)>16: self.pending.clear();raise AirSIMError('蓝牙音频分片不一致')
            parts[index]=data
            if sum(map(len,parts.values()))>528: self.pending.pop(ident);raise AirSIMError('蓝牙音频帧过长')
            if len(parts)==count:
                self.pending.pop(ident)
                pcm=mulaw_decode(media_crypt(key,2,magic[6],seq,stamp,b''.join(parts[i] for i in range(count)),True))
                if len(pcm)!=320: raise AirSIMError('蓝牙音频帧长度无效')
                self.seen[ident]=True
                if len(self.seen)>256:self.seen.popitem(last=False)
                frames.append((seq,pcm))
        return frames
