"""Measure PCM arrival timing only; no microphone samples are stored."""
import json
import statistics
import subprocess
import time

results={}
for label,prefix in [('buffered',[]),('unbuffered',['stdbuf','-o0'])]:
    command=prefix+['pw-cat','--record','--raw','--format','s16','--rate','8000','--channels','1','--latency','40ms','-']
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        times=[]
        for _ in range(65):
            assert len(process.stdout.read(320))==320
            times.append(time.monotonic())
        gaps=[(b-a)*1000 for a,b in zip(times,times[1:])]
        results[label]={'max_gap_ms':round(max(gaps),2),'median_gap_ms':round(statistics.median(gaps),2),'burst_gaps_under_1ms':sum(x<1 for x in gaps)}
    finally:
        process.terminate();process.wait(timeout=2)
print(json.dumps(results))
assert results['unbuffered']['burst_gaps_under_1ms']<results['buffered']['burst_gaps_under_1ms']
