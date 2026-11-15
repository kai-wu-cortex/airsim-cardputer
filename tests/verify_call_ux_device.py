"""Native framebuffer + evdev UX checks with an isolated in-memory phone/audio."""
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import time
from concurrent.futures import Future
from airsim_cp.app import App
from airsim_cp.hardware import Framebuffer,keyboard,character,KEY_CHARS
from airsim_cp.protocol import ProfileStore
from airsim_cp.ui import View
from test_call_ui import Phone,Audio

if os.geteuid()==0:raise SystemExit('Run as pi')
root=Path.home()/'.cache/airsim-ux-test';root.mkdir(mode=0o700,exist_ok=True)
spec=importlib.util.spec_from_file_location('helper','/home/pi/cardputer-dev/device_ui_test.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
subprocess.run(['systemctl','--user','stop','APPLaunch.service'],check=True)
phone=Phone();phone.profile['test_fixture']=True
app=App(ProfileStore(root),client=phone);app.audio.close();app.audio=Audio()
app.online=True;app.status={'device_name':'测试手机'};app.next_poll=float('inf')
fb=Framebuffer();keys=keyboard();view=View();checks=[]
def pump(seconds=.25):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        app.tick()
        if app.dirty:fb.show(view.render(app));app.dirty=False
        if select.select([keys],[],[],.02)[0]:
            for event in keys.read():
                if event.type==1 and event.value==1:
                    key={1:'ESC',28:'ENTER',15:'TAB',14:'BACKSPACE'}.get(event.code) or character(event.code,False)
                    if key:app.key(key)
def press(code):helper.inject(code,.05);pump()
def chars(value):
    mapping={v:k for k,v in KEY_CHARS.items()}
    for char in value:press(mapping[char])
def check(label,value):
    if not value:raise AssertionError(label)
    checks.append(label)
def snap(name):pump();helper.screenshot(str(root/(name+'.png')))
try:
    app.status.update(phone_number='10000',battery={'percent':76,'charging':True,'observed_at_ms':time.time()*1000})
    snap('home')
    check('home exposes phone number and battery',app.phone_number()=='10000' and app.battery_text()=='充电 76%')
    press(8);check('home 7 opens connection modes',app.page=='menu' and app.menu_title=='连接模式');snap('modes');press(1)
    press(6);check('home 5 opens local call history',app.menu_title=='通话记录');press(1)
    press(7);check('home 6 opens messages',app.page=='messages')
    press(9);chars('10000');press(15);chars('hi')
    check('compose draft entered using device keys',app.fields==['10000','hi'])
    phone.incoming();pump();check('incoming interrupts draft',app.page=='calls');snap('incoming')
    press(5);pump(.3)
    check('one key answers without confirmation',phone.actions==['answer'])
    check('answer starts audio automatically',app.audio.active);snap('active')
    press(5);check('one key toggles mute',app.audio.engine.muted);snap('muted')
    stale=phone.snapshot();blocked=Future();app.future=blocked;app.busy=True;app.work_kind='refresh'
    started=time.monotonic();press(9)
    hangup_seconds=time.monotonic()-started
    check('hangup bypasses unfinished refresh within 650 ms',phone.link.active is None and not blocked.done() and hangup_seconds<.65)
    blocked.set_result(stale);pump(.3)
    check('stale refresh cannot revive ended call',not app.active_call())
    check('one key hangs up',phone.actions==['answer','hangup'])
    check('draft restored after call',app.page=='compose' and app.fields==['10000','hi']);snap('draft')
    press(1);phone.incoming('second');pump();press(9);pump(.3)
    check('one key rejects incoming',phone.actions[-1]=='reject')
    check('reject does not start audio',len(app.audio.starts)==1)
    press(5);check('home 4 opens dial form',app.page=='dial');chars('10000');press(28)
    pump(.4);check('Enter dials without extra confirmation',phone.actions[-1]=='dial')
    check('outgoing connected audio is automatic',app.audio.active)
    snap('outgoing');press(9);pump(.3)
    check('call ended returns home',app.page=='home');press(9)
    check('home 8 opens connection settings',app.page=='settings');snap('connections')
    check('fixture never created real BLE client',type(app.client).__name__=='Phone')
finally:
    app.close();keys.close();fb.close()
    (root/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    subprocess.run(['systemctl','--user','start','APPLaunch.service'],check=True)
print(json.dumps({'passed':len(checks),'all_passed':True,'hangup_key_test_seconds':round(hangup_seconds,3),'phone_actions':phone.actions,'real_phone_touched':False}))
