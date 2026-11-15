"""Run as pi with the loopback fixture and isolated config active. No real phone traffic."""
import importlib.util
import json
import os
from pathlib import Path
import time

ROOT = Path.home() / '.cache/airsim-cardputer-test'
spec = importlib.util.spec_from_file_location('helper', '/home/pi/cardputer-dev/device_ui_test.py')
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
results = []

def state(): return json.loads((ROOT/'ui-state.json').read_text())
def check(label, predicate):
    end = time.monotonic() + 22
    while time.monotonic() < end:
        if predicate():
            results.append({'check': label, 'pass': True}); return
        time.sleep(0.1)
    raise AssertionError(label + ' ' + str(state()))
def page(name): check('screen ' + name, lambda: state()['page'] == name and not state()['busy'])
def key(code): helper.inject(code, 0.06); time.sleep(0.12)
def snap(name): helper.screenshot(str(ROOT/(name+'.png')))
def chars(value):
    from airsim_cp.hardware import KEY_CHARS
    mapping = {char: code for code, char in KEY_CHARS.items()}
    for char in value: key(mapping[char])

try:
    profile = json.loads((ROOT/'profile.json').read_text())
    assert profile['host'] == '127.0.0.1' and profile['test_fixture'] is True
    check('current UI process alive',lambda:Path('/proc/'+str(state()['pid'])+'/cmdline').exists())
    page('home'); check('ordinary user', lambda: state()['uid'] == 1000)
    check('real HTTP fixture connected', lambda: state()['online'])
    snap('home')
    key(7); page('messages'); key(28); page('message'); snap('message')
    key(8); page('compose'); key(1); page('home')
    key(7); page('messages');key(9);page('compose'); chars('10000'); key(15); chars('hello cp0'); key(28); page('confirm'); snap('sms-confirm')
    key(9); page('messages'); snap('sms-result'); key(1); page('home')
    key(5); page('dial'); chars('10000'); snap('dial-number');key(28);page('calls');time.sleep(1);snap('active-call')
    key(9); page('home')
    key(9); page('settings'); key(6); page('lan'); key(9); page('pair'); [key(14) for _ in range(15)]; chars('192.168.31.1'); key(15); chars('18080'); key(15); chars('123456')
    key(28); page('confirm'); snap('pair-local-confirm'); key(138); page('help'); key(1); page('confirm'); key(5); page('pair')
    key(1); page('lan'); key(1); page('settings'); key(1); page('home'); key(138); page('help'); snap('help'); key(1); page('home')
    before = len(list((ROOT/'screenshots').glob('*.png')))
    key(99); check('fn+J screenshot', lambda: len(list((ROOT/'screenshots').glob('*.png'))) > before)
    helper.inject(1, 1.05)
    import subprocess
    check('long ESC restores launcher', lambda: subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'APPLaunch.service']).returncode == 0)
finally:
    (ROOT/'device-checks.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
print(json.dumps({'passed': len(results), 'all_passed': True}))
