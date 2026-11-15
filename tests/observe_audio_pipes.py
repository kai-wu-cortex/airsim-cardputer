"""Read pipe occupancy only; root diagnostic, never reads PCM or credentials."""
import array,fcntl,json,os,statistics,termios,time
from pathlib import Path
samples=[];seen=False;last_active=0;start=time.monotonic();next_scan=0;targets=[]
while time.monotonic()-start<100:
 now=time.monotonic()
 if now>=next_scan:
  targets=[];next_scan=now+1
  for p in Path('/proc').glob('[0-9]*'):
   try:
    args=(p/'cmdline').read_bytes().split(b'\0')
    if args[0]!=b'pw-cat':continue
    if b'--playback' in args:targets.append((p,'playback','0'))
    elif b'--record' in args:targets.append((p,'capture','1'))
   except OSError:pass
 if targets:
  seen=True;last_active=now
  for p,kind,fdno in targets:
   fd=None
   try:
    fd=os.open(str(p/'fd'/fdno),os.O_RDONLY|os.O_NONBLOCK)
    count=array.array('i',[0]);fcntl.ioctl(fd,termios.FIONREAD,count,True)
    samples.append({'epoch':round(time.time(),3),'kind':kind,'queued_ms':count[0]/16})
   except OSError:pass
   finally:
    if fd is not None:os.close(fd)
 elif seen and now-last_active>2:break
 time.sleep(.05)
summary={}
for kind in ('playback','capture'):
 values=sorted(x['queued_ms'] for x in samples if x['kind']==kind)
 if values:summary[kind]={'samples':len(values),'max_ms':max(values),'median_ms':statistics.median(values),'p95_ms':values[int((len(values)-1)*.95)]}
result={'summary':summary,'samples':samples,'pcm_read':False}
Path('/tmp/airsim-pipe-observation.json').write_text(json.dumps(result))
print(json.dumps(summary),flush=True)
