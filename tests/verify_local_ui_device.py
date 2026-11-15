"""Native screen and key checks with an isolated profile; no real phone mutations."""
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
from airsim_cp.app import App
from airsim_cp.protocol import ProfileStore
from airsim_cp.hardware import Framebuffer,keyboard,character
from airsim_cp.ui import View

if os.geteuid()==0:raise SystemExit('Run as pi')
root=Path.home()/'.cache/airsim-local-ui-test';root.mkdir(mode=0o700,exist_ok=True)
spec=importlib.util.spec_from_file_location('helper','/home/pi/cardputer-dev/device_ui_test.py');helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
subprocess.run(['systemctl','--user','stop','APPLaunch.service'],check=True)
tmp=tempfile.TemporaryDirectory();app=App(ProfileStore(tmp.name),demo=True);fb=Framebuffer();keys=keyboard();view=View();checks=[]
def pump(seconds=.2):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  app.tick()
  if app.dirty:fb.show(view.render(app));app.dirty=False
  if select.select([keys],[],[],.03)[0]:
   for event in keys.read():
    if event.type==1 and event.value==1:
     key={1:'ESC',28:'ENTER',138:'HELP'}.get(event.code) or character(event.code,False)
     if key:app.key(key)
def press(code):helper.inject(code,.05);pump()
def check(label,ok):
 if not ok:raise AssertionError(label)
 checks.append(label)
try:
 pump();press(8)
 check('home 7 opens only Wi-Fi and BLE modes',app.page=='menu' and len(app.menu_items)==2 and 'Wi-Fi' in app.menu_items[0][0] and '蓝牙' in app.menu_items[1][0])
 helper.screenshot(str(root/'modes.png'))
 press(1);press(9);check('home 8 opens device connections',app.page=='settings')
 helper.screenshot(str(root/'connections.png'))
 press(8);check('connection key 7 opens modes',app.page=='menu' and app.menu_title=='连接模式')
 press(1);press(9);press(6);check('Wi-Fi discovery opens',app.page=='lan')
 press(9);check('manual connection remains available',app.page=='pair')
 app.fields=['127.0.0.1','18080','123456'];app.field=2;press(28)
 check('pairing asks for local mode confirmation without cloud config',app.page=='confirm' and '本地模式' in app.confirm_body and not app.busy)
 helper.screenshot(str(root/'pair-confirmation.png'))
 press(5);check('cancel does not save or pair',app.page=='pair' and app.store.profiles()==[])
 press(1);press(1);press(1);check('ESC returns home',app.page=='home')
 press(138);check('system help key opens help',app.page=='help')
 press(1);check('help returns home',app.page=='home')
 helper.screenshot(str(root/'home.png'))
finally:
 app.close();keys.close();fb.close();tmp.cleanup()
 (root/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
 subprocess.run(['systemctl','--user','start','APPLaunch.service'],check=True)
print(json.dumps({'passed':len(checks),'all_passed':True,'real_phone_mutations':False}))
