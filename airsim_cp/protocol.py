"""AirSIM protocol adapter. No dependency on or writes to the AirSIM source tree."""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import time
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class AirSIMError(Exception):
    def __init__(self,message,*,status=None):
        super().__init__(message);self.status=status


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def unb64(value):
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


def mac(secret, canonical):
    return b64(hmac.new(secret, canonical.encode(), hashlib.sha256).digest())


def canonical(method, path, body, timestamp, nonce):
    return f'{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}\n{timestamp}\n{nonce}'


def valid_host(host):
    try:
        address = ipaddress.IPv4Address(host)
        ranges = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', '127.0.0.0/8')
        if not any(address in ipaddress.IPv4Network(net) for net in ranges):
            raise ValueError()
        return str(address)
    except ValueError:
        raise AirSIMError('请输入局域网或 Tailscale IPv4 地址') from None


def valid_port(value):
    try:
        port = int(value)
        if not 1024 <= port <= 65535:
            raise ValueError()
        return port
    except (ValueError, TypeError):
        raise AirSIMError('端口范围为 1024–65535') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Transport:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, host, port, method, path, body=b'', headers=None, timeout=8):
        url = f'http://{valid_host(host)}:{valid_port(port)}{path}'
        request = urllib.request.Request(url, data=body if method == 'POST' else None,
                                         headers=headers or {}, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise AirSIMError('响应超过大小限制')
                return json.loads(data)
        except urllib.error.HTTPError as error:
            labels = {401: '认证失败：检查时间或重新配对', 403: '手机拒绝此网络来源',
                      404: '手机不支持此接口', 409: '通话状态已变化，请刷新',
                      410: '配对或通话已过期', 429: '请求过于频繁，请稍后再试'}
            raise AirSIMError(labels.get(error.code, f'手机返回 HTTP {error.code}'),status=error.code) from None
        except (OSError, urllib.error.URLError, TimeoutError):
            suffix = '；结果未知，请先在手机核实，勿重复操作' if method == 'POST' else ''
            raise AirSIMError('无法连接手机或请求超时' + suffix) from None
        except (ValueError, UnicodeError):
            raise AirSIMError('手机返回了无效数据') from None


def profile_identity(profile):
    if profile.get('transport')=='ble':return 'ble|'+str(profile.get('agent_id',''))
    return '|'.join(str(profile.get(key,'')) for key in ('transport','agent_id','host','ble_address','relay_url','device_id'))


def validate_profile(profile):
    if not isinstance(profile,dict) or not isinstance(profile.get('agent_id'),str) or not profile['agent_id']:
        raise ValueError('missing identity')
    mode=profile.get('transport','lan')
    if mode=='ble':
        if not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}',profile.get('ble_address','')) or len(unb64(profile['ble_key']))!=32:
            raise ValueError('invalid BLE profile')
    elif mode=='lan':
        valid_host(profile['host']);valid_port(profile['control_port']);valid_port(profile['pcm_port'])
        if len(unb64(profile['secret']))!=32:raise ValueError('invalid LAN key')
    else:raise ValueError('unknown transport')


class ProfileStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path.home() / '.config/airsim-cardputer'
        self.path = self.directory / 'profile.json'

    def load(self):
        if not self.path.exists():
            return None
        try:
            profile = json.loads(self.path.read_text())
            if profile.get('transport')=='cloud':
                routes=self.profiles()
                return routes[-1] if routes else None
            validate_profile(profile)
            return profile
        except (KeyError, ValueError, OSError):
            raise AirSIMError('配对配置损坏，请重新配对') from None

    def ble_pending(self,agent):
        return self.load_data('ble-pending.json',{}).get(agent)

    def save_ble_pending(self,profile):
        validate_profile(profile)
        pending=self.load_data('ble-pending.json',{})
        pending[profile['agent_id']]=profile
        self.save_data('ble-pending.json',pending)

    def clear_ble_pending(self,agent):
        pending=self.load_data('ble-pending.json',{})
        pending.pop(agent,None)
        self.save_data('ble-pending.json',pending)

    def save(self, profile):
        validate_profile(profile)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        profiles=self.profiles()
        identity=profile_identity(profile)
        profiles=[item for item in profiles if profile_identity(item)!=identity]+[profile]
        self.save_data('profiles.json',profiles)
        temporary = self.directory / ('profile-' + secrets.token_hex(6) + '.tmp')
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(profile, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


    def load_data(self,name,default):
        path=self.directory/name
        if not path.exists():return default
        if path.stat().st_size>4*1024*1024:raise AirSIMError('本地数据过大')
        return json.loads(path.read_text())

    def save_data(self,name,value):
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(self.directory,0o700)
        path=self.directory/name;temp=self.directory/(name+'.'+secrets.token_hex(6)+'.tmp')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
            os.replace(temp,path)
        finally:temp.unlink(missing_ok=True)

    def profiles(self):
        values=self.load_data('profiles.json',[])
        if not isinstance(values,list):raise AirSIMError('连接列表损坏')
        if not values and self.path.exists():values=[json.loads(self.path.read_text())]
        values=[p for p in values if not isinstance(p,dict) or p.get('transport')!='cloud']
        for profile in values:validate_profile(profile)
        return values

    def history_path(self, agent_id):
        name = hashlib.sha256(agent_id.encode()).hexdigest()[:24]
        return self.directory / ('messages-' + name + '.json')

    def load_messages(self, agent_id):
        path = self.history_path(agent_id)
        if not path.exists():
            return []
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError()
            messages = json.loads(path.read_text())
            if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
                raise ValueError()
            return messages[:512]
        except (ValueError, OSError):
            raise AirSIMError('本地短信记录无法读取') from None

    def save_messages(self, agent_id, messages):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        path = self.history_path(agent_id)
        temp = path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(messages[:512], stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


class Client:
    def __init__(self, profile=None, transport=None):
        self.profile = profile
        self.transport = transport or Transport()
        self.sms_feed=None
        self.closed=False

    @property
    def secret(self):
        if not self.profile:
            raise AirSIMError('请先配对安卓手机')
        return unb64(self.profile['secret'])

    def signed(self, method, path, payload=None):
        body = b'' if payload is None else json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()
        nonce = b64(secrets.token_bytes(16))
        timestamp = int(time.time() + self.profile.get('clock_offset', 0))
        signature = mac(self.secret, canonical(method, path, body, timestamp, nonce))
        headers = {'Content-Type': 'application/json', 'X-AirSIM-VoWLAN-Version': '1',
                   'X-AirSIM-VoWLAN-Timestamp': str(timestamp), 'X-AirSIM-VoWLAN-Nonce': nonce,
                   'X-AirSIM-VoWLAN-Signature': signature}
        result = self.transport.request(self.profile['host'], self.profile['control_port'], method,
                                        path, body, headers, timeout=18 if method == 'POST' else 6)
        return result, nonce

    def health(self):
        result, nonce = self.signed('GET', '/v1/health')
        expected = mac(self.secret, f"AIRSIM-HEALTH1\n{nonce}\n{self.profile['agent_id']}")
        if (result.get('agent_id') != self.profile['agent_id'] or result.get('ok') is not True
                or not hmac.compare_digest(expected, str(result.get('identity_proof', '')))):
            raise AirSIMError('手机身份验证失败，已停止连接')
        return result

    def snapshot(self):
        health=self.health()
        calls, _ = self.signed('GET', '/api/calls/status')
        sms, _ = self.signed('GET', '/api/sms')
        # Android's direct gateway supplies metadata in authenticated health;
        # /api/android/status belongs to other Agent implementations.
        status = {} if health.get('transport')=='vowlan' else self.signed('GET', '/api/android/status')[0]
        if isinstance(status,dict):
            for key in ('sim_slots','battery','phone_number','device_name','model'):
                if key in health:status[key]=health[key]
        if not isinstance(sms, list) or not isinstance(calls, dict) or not isinstance(status, dict):
            raise AirSIMError('手机数据格式不兼容')
        # Do not ACK/delete the shared delivery queue: existing AirSIM clients still need it.
        return {'calls': calls, 'sms': sms, 'status': status}

    def pair(self, host, port, code, *, accept_local_mode=False):
        if not accept_local_mode:
            raise AirSIMError('请先确认本地配对会更改手机的云端注册')
        host, port = valid_host(host), valid_port(port)
        if not re.fullmatch(r'[0-9]{6}', code):
            raise AirSIMError('请输入六位配对码')
        started = time.time()
        info = self.transport.request(host, port, 'GET', '/pair/v1/info')
        if info.get('product') != 'airsim-pair' or info.get('version') != 1:
            raise AirSIMError('不是兼容的 AirSIM 配对窗口')
        server_time = float(info.get('server_time', time.time() * 1000)) / 1000
        offset = server_time - (started + time.time()) / 2
        if float(info['expires_at']) / 1000 <= time.time() + offset:
            raise AirSIMError('配对窗口已过期，请重新打开')
        session = info['session_id']
        private = X25519PrivateKey.generate()
        public = b64(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        shared = private.exchange(X25519PublicKey.from_public_bytes(unb64(info['server_public_key'])))
        key = HKDF(hashes.SHA256(), 32, session.encode(), b'AirSIMPair/v1:' + code.encode()).derive(shared)
        secret, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
        registration = json.dumps(dict(local_mode=True,cloud_enabled=False,vowlan_secret=b64(secret))).encode()
        sealed = nonce + AESGCM(key).encrypt(nonce, registration, session.encode())
        claim = {'version': 1, 'session_id': session, 'client_public_key': public,
                 'sealed_registration': b64(sealed), 'want_direct_route': True}
        result = self.transport.request(host, port, 'POST', '/pair/v1/claim', json.dumps(claim).encode(),
                                        {'Content-Type': 'application/json'}, timeout=18)
        try:
            control, pcm = valid_port(result['control_port']), valid_port(result['pcm_port'])
            agent = result['agent_id']
            expected = mac(secret, f'AIRSIM-DIRECT1\n{session}\n{public}\n{agent}\n{control}\n{pcm}')
            if not hmac.compare_digest(expected, result.get('route_proof', '')):
                raise AirSIMError('手机端口签名不匹配，未保存配对')
        except KeyError:
            raise AirSIMError('手机已接收密钥但网关未就绪；检查 Shizuku 后重新配对') from None
        profile = {'host': host, 'control_port': control, 'pcm_port': pcm, 'agent_id': agent,
                   'secret': b64(secret), 'clock_offset': offset}
        candidate = Client(profile, self.transport)
        candidate.health()
        return profile

    def send_sms(self, phone, message):
        if not re.fullmatch(r'[+0-9*# ()-]{1,40}', phone) or not message.strip() or len(message) > 2000:
            raise AirSIMError('请检查收件号码和短信正文')
        self.health()
        return self.signed('POST', '/api/sms/send', {'phone': phone, 'message': message, **self.sim_fields()})[0]

    def dial(self, number):
        if not re.fullmatch(r'[+0-9*# ()-]{1,40}', number):
            raise AirSIMError('请输入有效的电话号码')
        self.health()
        operation = str(uuid.uuid4())
        result, nonce = self.signed('POST', '/api/calls/dial', {'number': number, 'operation_id': operation, **self.sim_fields()})
        expected = mac(self.secret, f"AIRSIM-DIAL1\n{nonce}\n{operation}\n{result.get('android_id', '')}\n{result.get('backend_id', '')}")
        if (result.get('operation_id') != operation or not result.get('android_id') or not result.get('backend_id')
                or not hmac.compare_digest(expected, result.get('operation_proof', ''))):
            raise AirSIMError('拨号回执未验证；请在手机核实，勿重复拨号')
        return result

    def control(self, action, call_id):
        if action not in ('answer', 'reject', 'hangup') or not call_id:
            raise AirSIMError('没有可操作的通话')
        self.health()
        return self.signed('POST', '/api/calls/' + action, {'call_id': call_id})[0]

    def sim_fields(self):
        return {'sim_account':self.profile['sim_account']} if self.profile.get('sim_account') else {}

    def dtmf(self,digit,call_id):
        if digit not in '0123456789*#' or len(digit)!=1 or not call_id:raise AirSIMError('无效 DTMF')
        self.health()
        return self.signed('POST','/api/calls/dtmf',{'digit':digit,'call_id':call_id})[0]

    def start_sms_watch(self):
        if self.closed or not self.profile or self.sms_feed is not None:return
        from .sms_feed import SMSFeed
        self.sms_feed=SMSFeed(Client(dict(self.profile)))

    def pending_sms(self):
        return self.sms_feed.drain() if self.sms_feed is not None else []

    def close(self):
        self.closed=True
        if self.sms_feed:self.sms_feed.close()

