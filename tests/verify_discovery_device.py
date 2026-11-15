"""Real mDNS/framebuffer/evdev checks, with a synthetic pairing advertisement only."""
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import tempfile
import time
from zeroconf import Zeroconf, ServiceInfo, IPVersion
from airsim_cp.app import App
from airsim_cp.discovery import PAIR, GATEWAY
from airsim_cp.hardware import Framebuffer, keyboard, character
from airsim_cp.protocol import ProfileStore
from airsim_cp.ui import View

if os.geteuid()==0:raise SystemExit('Run as pi')
root=Path.home()/'.cache/airsim-discovery-test';root.mkdir(mode=0o700,exist_ok=True)
spec=importlib.util.spec_from_file_location('helper','/home/pi/cardputer-dev/device_ui_test.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
zc=Zeroconf(ip_version=IPVersion.V4Only)
service=ServiceInfo(PAIR,'Cardputer Discovery Test.'+PAIR,addresses=[socket.inet_aton('127.0.0.1')],port=45678,
 properties={'v':'1','expires':str(int(time.time()*1000+120000))},server='cardputer-discovery-test.local.')
zc.register_service(service)
gateway=ServiceInfo(GATEWAY,'Cardputer Gateway Test.'+GATEWAY,addresses=[socket.inet_aton('127.0.0.1')],port=45679,
 properties={'v':'1','agent_id':'fixture','device_name':'Cardputer Discovery Test','pcm_port':'45680'},server='cardputer-discovery-test.local.')
zc.register_service(gateway)
subprocess.run(['systemctl','--user','stop','APPLaunch.service'],check=True)
tmp=tempfile.TemporaryDirectory();app=App(ProfileStore(tmp.name));app.next_poll=float('inf')
fb=Framebuffer();keys=keyboard();view=View();checks=[]
def pump(seconds=.25):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        app.tick()
        if app.dirty:fb.show(view.render(app));app.dirty=False
        if select.select([keys],[],[],.02)[0]:
            for event in keys.read():
                if event.type==1 and event.value==1:
                    key={1:'ESC',28:'ENTER',15:'TAB',14:'BACKSPACE'}.get(event.code) or character(event.code,False)
                    if key:app.key(key)
def press(code):helper.inject(code,.05);pump()
def check(label,value):
    if not value:raise AssertionError(label)
    checks.append(label)
def snap(name):pump();helper.screenshot(str(root/(name+'.png')))
try:
    press(9);check('home 8 opens connections',app.page=='settings');snap('connections')
    press(6);check('Wi-Fi starts discovery',app.page=='lan' and app.lan_scan is not None)
    first=app.lan_scan;press(9)
    check('bottom 8 opens manual during scan',app.page=='pair' and app.lan_scan is None)
    first.thread.join(3);check('cancel closes discovery thread',not first.thread.is_alive())
    snap('manual');press(5);check('manual digits remain input',app.fields[0]=='4')
    press(1);check('ESC returns to discovery',app.page=='lan')
    until=time.monotonic()+10
    while time.monotonic()<until and not any(d['name']=='Cardputer Discovery Test' for d in app.lan_devices):pump(.2)
    found=next((i for i,d in enumerate(app.lan_devices) if d['name']=='Cardputer Discovery Test'),None)
    check('real mDNS resolved synthetic pairing service',found is not None)
    pump(1)
    matches=[d for d in app.lan_devices if d['host']=='127.0.0.1']
    check('gateway and pairing merged into one phone',len(matches)==1 and matches[0].get('pairing') is not None)
    found=next(i for i,d in enumerate(app.lan_devices) if d['host']=='127.0.0.1')
    for _ in range(found):press(45)  # X = down
    check('F/X navigates discovered devices',app.lan_index==found)
    snap('discovered');press(7)
    check('bottom 6 selects discovered address',app.page=='pair' and app.fields[:2]==['127.0.0.1','45678'])
    check('code is focused',app.field==2);snap('code')
    check('discovery never paired or saved a profile',not app.client.profile and not app.store.profiles())
    press(1);scan=app.lan_scan;press(1)
    check('ESC returns to connection settings',app.page=='settings' and app.lan_scan is None)
    scan.thread.join(3);check('leaving discovery releases browser',not scan.thread.is_alive())
finally:
    app.close();keys.close();fb.close();tmp.cleanup()
    zc.unregister_service(gateway);zc.unregister_service(service);zc.close()
    (root/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    subprocess.run(['systemctl','--user','start','APPLaunch.service'],check=True)
print(json.dumps({'passed':len(checks),'real_phone_paired':False,'all_passed':True}))
