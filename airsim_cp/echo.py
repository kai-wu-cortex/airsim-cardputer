"""Per-call PipeWire/WebRTC AEC; never changes the system defaults or mixer."""
import json
import re
import secrets
import subprocess
import threading
import time
from .protocol import AirSIMError
from .playback import playback_command


class EchoRoute:
    def __init__(self):
        self.name = 'airsim-aec-' + secrets.token_hex(5)
        self.process = None
        self.closed = False
        self.lock = threading.Lock()

    @staticmethod
    def default_name(kind):
        result = subprocess.check_output(['wpctl', 'inspect', '@DEFAULT_AUDIO_' + kind + '@'],
                                         text=True, timeout=2)
        match = re.search(r'node.name\s*=\s*"([^"\n]+)"', result)
        if not match:
            raise AirSIMError('系统音频设备不可用')
        return match[1]

    def start(self, cancelled):
        if self.closed or cancelled.is_set():
            raise AirSIMError('语音已取消')
        source, sink = self.default_name('SOURCE'), self.default_name('SINK')
        common = {'application.name': 'AirSIM Cardputer', 'state.restore-props': False,
                  'audio.channels': 1, 'audio.position': ['MONO']}
        args = {'library.name': 'aec/libspa-aec-webrtc', 'audio.rate': 48000,
                'audio.channels': 1, 'audio.position': ['MONO'], 'node.latency': '480/48000',
                'aec.args': {'webrtc.gain_control': False, 'webrtc.noise_suppression': True,
                             'webrtc.high_pass_filter': True},
                'capture.props': dict(common, **{'node.name': self.name+'.capture', 'target.object': source}),
                'playback.props': dict(common, **{'node.name': self.name+'.playback', 'target.object': sink}),
                'source.props': dict(common, **{'node.name': self.name+'.source', 'priority.session': 0}),
                'sink.props': dict(common, **{'node.name': self.name+'.sink', 'priority.session': 0})}
        with self.lock:
            if self.closed or cancelled.is_set():
                raise AirSIMError('语音已取消')
            self.process = subprocess.Popen(['pw-cli'], stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             text=True)
            self.process.stdin.write('load-module libpipewire-module-echo-cancel '+json.dumps(args)+'\n')
            self.process.stdin.flush()
        deadline = time.monotonic()+3
        while time.monotonic() < deadline and not cancelled.is_set():
            if self.closed or self.process.poll() is not None:
                break
            nodes = json.loads(subprocess.check_output(['pw-dump'], timeout=2))
            names = {n.get('info', {}).get('props', {}).get('node.name') for n in nodes}
            if all(self.name+suffix in names for suffix in ('.capture', '.source', '.sink', '.playback')):
                return
            cancelled.wait(.05)
        raise AirSIMError('回声消除启动失败，请重新连接声音')

    def playback(self):
        return playback_command()[:-1] + ['--target', self.name+'.sink', '-']

    def capture(self):
        return ['stdbuf', '-o0', 'pw-cat', '--record', '--raw', '--format', 's16',
                '--rate', '8000', '--channels', '1', '--latency', '40ms',
                '--target', self.name+'.source', '--properties',
                '{ application.name = "AirSIM Cardputer" state.restore-props = false }', '-']

    def close(self):
        with self.lock:
            self.closed = True
            process = self.process
        if process is not None:
            try:
                process.terminate()
                try: process.wait(timeout=.5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=.5)
            except ProcessLookupError:
                pass
            if process.stdin:
                try: process.stdin.close()
                except OSError: pass
