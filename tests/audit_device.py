"""Read-only final artifact/runtime audit. Does not read credential contents."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

pid = None
for entry in Path('/proc').glob('[0-9]*'):
    try:
        command = (entry/'cmdline').read_bytes().split(b'\0')
        if command[:3] == [b'/usr/bin/python3', b'-m', b'airsim_cp.app']:
            pid = int(entry.name); break
    except (OSError, PermissionError): pass
if pid is None: raise SystemExit('App is not running')

def ticks():
    words=Path(f'/proc/{pid}/stat').read_text().split(); return int(words[13])+int(words[14])
start=time.monotonic(); before=ticks(); time.sleep(5); cpu=(ticks()-before)/os.sysconf('SC_CLK_TCK')/(time.monotonic()-start)*100
status=Path(f'/proc/{pid}/status').read_text().splitlines()
uid=next(line.split()[1] for line in status if line.startswith('Uid:'))
rss=next(line.split(':',1)[1].strip() for line in status if line.startswith('VmRSS:'))
base=Path('/usr/share/airsim-cardputer/airsim_cp')
files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(base.glob('*.py'))}
result={'pid':pid, 'uid':int(uid), 'idle_cpu_percent_one_core':round(cpu,2), 'rss':rss,
        'installed_source_sha256':files,
        'package':subprocess.check_output(['dpkg-query','-W','-f=${Package} ${Version} ${Architecture} ${Status}','airsim-cardputer']).decode(),
        'production_profile_exists':(Path.home()/'.config/airsim-cardputer/profile.json').exists(),
        'fixture_active':subprocess.run(['systemctl','--user','is-active','--quiet','airsim-fixture.service']).returncode==0,
        'test_service_active':subprocess.run(['systemctl','--user','is-active','--quiet','airsim-cardputer-test.service']).returncode==0,
        'unexpected_writable_package_files':[str(p) for p in base.glob('*.py') if p.stat().st_mode & 0o022]}
print(json.dumps(result,indent=2))
