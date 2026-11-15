"""Synthetic PCM -> private virtual sink -> monitor; never captures microphone."""
import array
import fcntl
import json
import os
import struct
import subprocess
import termios
import threading
import time

if os.geteuid()==0:raise SystemExit('Run as pi')
def pactl(*args):return subprocess.check_output(['pactl',*args],text=True,timeout=10).strip()
sink='airsim_latency_probe'
defaults={kind:pactl('get-default-'+kind) for kind in ('sink','source')}
module=pactl('load-module','module-null-sink','sink_name='+sink,'rate=8000','channels=1','channel_map=mono','sink_properties=priority.session=0')
results={}
try:
    for label,command in (
        ('pw_cat',['pw-cat','--playback','--target='+sink,'--raw','--format','s16','--rate','8000','--channels','1','--latency','40ms','-']),
        ('pacat',['pacat','--playback','--device='+sink,'--raw','--format=s16le','--rate=8000','--channels=1','--latency-msec=60','--process-time-msec=20'])):
        sent=[];seen=[];pending=[];stop=threading.Event()
        recorder=subprocess.Popen(['parec','--device='+sink+'.monitor','--raw','--format=s16le','--rate=8000','--channels=1','--latency-msec=10','--process-time-msec=10'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        player=subprocess.Popen(command,stdin=subprocess.PIPE,stderr=subprocess.DEVNULL)
        def record():
            last=0
            while not stop.is_set():
                pcm=recorder.stdout.read(160)
                if len(pcm)!=160:return
                peak=max(abs(x[0]) for x in struct.iter_unpack('<h',pcm));now=time.monotonic()
                if peak>8000 and now-last>.2:seen.append(now);last=now
        thread=threading.Thread(target=record,daemon=True);thread.start()
        try:
            start=time.monotonic()
            for index in range(350):
                time.sleep(max(0,start+index*.02-time.monotonic()))
                marker=index in (50,150,250)
                if marker:sent.append(time.monotonic())
                player.stdin.write(struct.pack('<h',16000 if marker else 0)*160);player.stdin.flush()
                count=array.array('i',[0]);fcntl.ioctl(player.stdin.fileno(),termios.FIONREAD,count,True);pending.append(count[0])
            time.sleep(.3)
            results[label]={'markers_sent':len(sent),'markers_received':len(seen),'latency_ms':[round((b-a)*1000,1) for a,b in zip(sent,seen)],'max_pipe_ms':max(pending)/16}
            print(json.dumps({label:results[label]}),flush=True)
        finally:
            stop.set()
            player.stdin.close()
            for process in (player,recorder):process.terminate()
            for process in (player,recorder):
                try:process.wait(timeout=1)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=1)
            thread.join(timeout=1)
    print(json.dumps(results),flush=True)
finally:
    pactl('unload-module',module)
    for kind,previous in defaults.items():
        if pactl('get-default-'+kind).startswith(sink):pactl('set-default-'+kind,previous)
