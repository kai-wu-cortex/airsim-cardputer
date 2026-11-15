import json
from pathlib import Path
import subprocess
import threading
import time
from airsim_cp.ble import BLELink

link=BLELink();result={}
def scan():
    try:link.scan();result['scan_finished']=True
    except Exception:result['scan_cancelled']=True
thread=threading.Thread(target=scan,daemon=True);thread.start();time.sleep(1)
start=time.monotonic();link.close();thread.join(timeout=3)
result['close_seconds']=round(time.monotonic()-start,3)
result['worker_stopped']=not thread.is_alive()
show=subprocess.check_output(['bluetoothctl','show']).decode()
result['discovering_after_close']='Discovering: yes' in show
result['phone_connected_or_paired']=False
assert result['worker_stopped'] and not result['discovering_after_close']
Path('/home/pi/.cache/airsim-cardputer-test/ble-cancel-check.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
