"""Authenticated, read-only real-phone mode round trip; no calls, SMS or pairing."""
import json
import time
from pathlib import Path
from airsim_cp.app import App
from airsim_cp.protocol import ProfileStore

app=App(ProfileStore());results=[]
original=app.client.profile
assert original and original.get('transport','lan')=='lan'
assert any(p.get('transport')=='ble' and p['agent_id']==original['agent_id'] for p in app.store.profiles())

def ready(mode):
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        app.tick()
        if app.online and not app.busy and app.client.profile.get('transport','lan')==mode:
            assert not app.active_call(),'User call active; stop mode test'
            results.append(dict(mode=mode,authenticated=True,agent_matches=app.client.profile['agent_id']==original['agent_id'],
                                battery_present=bool(app.status.get('battery')),phone_number_present=app.phone_number()!='未提供'))
            print(json.dumps(results[-1]),flush=True);return
        time.sleep(.08)
    raise AssertionError('Mode did not become ready: '+mode)
try:
    ready('lan')
    app.mode_menu();app.choose_mode('ble');ready('ble')
    app.mode_menu();app.choose_mode('lan');ready('lan')
finally:
    app.close()
    # Preserve any rotated BLE key saved during authentication; restore only the active LAN route.
    if not app.active_call():app.store.save(original)
    path=Path.home()/'.cache/airsim-mode-checks.json';path.write_text(json.dumps(results,indent=2))
print(json.dumps({'passed':len(results)==3,'real_phone_calls':False,'real_sms_sent':False}))
