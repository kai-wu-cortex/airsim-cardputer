"""Read-only phone status probe using the existing pairing. No calls or SMS."""
import json
import time
from pathlib import Path
from airsim_cp.protocol import ProfileStore
from airsim_cp.ble import BLEClient

store=ProfileStore();profile=store.load()
assert profile['transport']=='ble'
client=BLEClient(profile,store);results={'samples':[],'phone_actions':['device_info']}
try:
    client.health()
    results['mtu']=client.link.client.mtu_size
    for _ in range(8):
        start=time.monotonic();client.link.info_at=0
        client.health()
        results['samples'].append({'seconds':round(time.monotonic()-start,3),'connected':client.link.ready})
        time.sleep(5)
finally:
    client.close()
    Path('/home/pi/.cache/airsim-ble-latency.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results))
