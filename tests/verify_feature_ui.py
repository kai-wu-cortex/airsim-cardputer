"""Real framebuffer/key tests for new menus; never confirms phone pairing ."""
import importlib.util
import json
from pathlib import Path
import time
import subprocess
from airsim_cp.hardware import KEY_CHARS

ROOT=Path.home()/'.cache/airsim-cardputer-test'
spec=importlib.util.spec_from_file_location('helper','/home/pi/cardputer-dev/device_ui_test.py');helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
checks=[]
def state():return json.loads((ROOT/'ui-state.json').read_text())
def check(label,predicate):
    end=time.monotonic()+22
    while time.monotonic()<end:
        if predicate():checks.append({'check':label,'pass':True});return
        time.sleep(.1)
    raise AssertionError(label+str(state()))
def page(name):check('screen '+name,lambda:state()['page']==name and not state()['busy'])
def key(code):helper.inject(code,.06);time.sleep(.15)
def chars(text):
    mapping={v:k for k,v in KEY_CHARS.items()}
    for char in text:key(mapping[char])
def snap(name):helper.screenshot(str(ROOT/(name+'.png')))
try:
    check('current UI process alive',lambda:Path('/proc/'+str(state()['pid'])+'/cmdline').exists())
    page('home');key(9);page('settings');snap('connections-v2')
    key(8);page('menu');snap('local-modes');key(1);page('home');key(9);page('settings');key(7)
    check('BlueZ scan UI completes',lambda:state()['page'] in ('menu','notice') and not state()['busy'])
    snap('bluetooth-scan-v2');key(1);page('home')
    key(9);page('settings');key(9);page('menu');snap('more-v2')
    key(7);key(7);key(28);page('menu');key(28);page('contact');chars('fixture');key(15);chars('10000');key(28);page('menu')
    key(7);key(28);page('menu');key(28);page('dial');snap('contact-dial-v2');key(1);page('home')
    key(9);page('settings');key(9);page('menu');key(7);key(7);key(7);key(28);page('menu');snap('history-v2')
    helper.inject(1,1.05)
    check('long ESC restores launcher',lambda:subprocess.run(['systemctl','--user','is-active','--quiet','APPLaunch.service']).returncode==0)
finally:(ROOT/'feature-ui-checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
print(json.dumps({'passed':len(checks),'all_passed':True}))
