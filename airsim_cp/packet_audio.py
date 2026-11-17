"""PipeWire audio for BLE; never falls back to another call/transport mid-call."""
import asyncio
import logging
import queue
import subprocess
import threading
import time
from .audio import AudioSession
from .early_audio import EarlyAudio
from .playback import SpeechPlayback, playback_command
from .echo import EchoRoute
from .protocol import AirSIMError
from . import ble_wire as wire


class PacketAudio:
    def __init__(self):
        self.active=False;self.error='';self.uplink_bytes=self.downlink_bytes=0
        self.stop_event=threading.Event();self.lock=threading.RLock();self.processes=[];self.threads=[]
        self.client=None;self.call=None;self.closed=False;self.muted=False;self.media_started=False
        self.frames=queue.Queue(maxsize=3);self.capture_frames=queue.Queue(maxsize=3)
        self.reader=wire.AudioReader();self.sequence=0;self.timestamp=0;self.suppress_release=False
        self.capture_drops=0;self.playback_drops=0
        self.received_frames=0;self.receive_gap_ms=0;self.received_at=None;self.started_at=None
        self.reported=False;self.route=None
    def _received(self,data):
        if not self.active:return
        try:
            for sequence,pcm in self.reader.append(self.key,data):
                now=time.monotonic()
                if self.received_at is not None:self.receive_gap_ms=max(self.receive_gap_ms,(now-self.received_at)*1000)
                self.received_at=now;self.received_frames+=1
                if self.frames.full():
                    try:self.frames.get_nowait();self.playback_drops+=1
                    except queue.Empty:pass
                self.frames.put_nowait((sequence,pcm))
        except Exception:pass
    def start(self,client,call_id):
        self.client=client
        mode=client.profile.get('transport')
        snapshot=client.snapshot();call=snapshot['calls'].get('active')
        if not call or call['id']!=call_id or call['state'] not in ('active','connected','answered'):
            raise AirSIMError('请在电话接通后开启语音')
        self.call=call
        if self.closed:raise AirSIMError('应用已退出')
        if mode=='ble':
            link=client.link
            response=link.command('start_media',call_id,call['generation'],{'audio_notification_batch_v1':True})
            if response.get('detail')!='media_ready':raise AirSIMError('手机蓝牙音频未就绪')
            self.media_started=True
            if self.closed:
                self.stop();raise AirSIMError('应用已退出')
            self.key=wire.session_key(link.key,link.session,call_id,call['generation'],b'media')
            link.on_audio=self._received
        else:raise AirSIMError('不支持的音频连接方式')
        try:
            with self.lock:
                if self.closed:raise AirSIMError('语音已取消')
                self.route=EchoRoute()
            self.route.start(self.stop_event)
            with self.lock:
                if self.closed:raise AirSIMError('应用已退出')
                self.stop_event.clear()
                playback=subprocess.Popen(self.route.playback(),stdin=subprocess.PIPE,stderr=subprocess.DEVNULL)
                self.processes.append(playback)
                # pw-cat uses stdio: a pipe otherwise buffers ~256 ms of 8 kHz
                # PCM before flushing, defeating our 20 ms frames and queue cap.
                capture=subprocess.Popen(self.route.capture(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
                self.processes.append(capture);self.active=True;self.started_at=time.monotonic()
            microphone=SpeechPlayback(gain_db=9)
            def record():
                while not self.stop_event.is_set():
                    pcm=capture.stdout.read(320)
                    if len(pcm)!=320:raise AirSIMError('麦克风不可用')
                    pcm=bytes(320) if self.muted else microphone.process(pcm)
                    frame=(self.sequence,self.timestamp,pcm)
                    self.sequence+=1;self.timestamp=(self.timestamp+160)&0xffffffff
                    if self.sequence>=0xffffffff:raise AirSIMError('语音会话已达到序号上限')
                    if self.capture_frames.full():
                        try:self.capture_frames.get_nowait();self.capture_drops+=1
                        except queue.Empty:pass
                    self.capture_frames.put_nowait(frame)
            def upload():
                while not self.stop_event.is_set():
                    sequence,timestamp,pcm=self.capture_frames.get(timeout=8)
                    if self.stop_event.is_set():break
                    if mode=='ble':
                        mtu=max(20,min(512,client.link.client.mtu_size-3))
                        packets=wire.audio_fragments(self.key,sequence,timestamp,pcm,mtu)
                        async def send():
                            for packet in packets:
                                if self.stop_event.is_set():return
                                await client.link.client.write_gatt_char(wire.AUDIO_UP,packet,response=False)
                        client.link.run(send(),2)
                    self.uplink_bytes+=320
            speech=SpeechPlayback()
            def download():
                last_sequence=-1
                while not self.stop_event.is_set():
                    if mode=='ble':
                        sequence,pcm=self.frames.get(timeout=8)
                        if sequence<=last_sequence:continue
                        last_sequence=sequence
                    playback.stdin.write(speech.process(pcm));playback.stdin.flush();self.downlink_bytes+=len(pcm)
            def worker(function):
                try:function()
                except Exception as error:
                    if not self.stop_event.is_set():
                        logging.getLogger(__name__).warning('Audio worker=%s failed type=%s',function.__name__,type(error).__name__)
                        self.error='音频中断，请检查连接和手机通话状态'
                finally:self.stop()
            with self.lock:
                if self.closed:raise AirSIMError('应用已退出')
                self.threads=[threading.Thread(target=worker,args=(f,),daemon=True) for f in (record,upload,download)]
                for thread in self.threads:thread.start()
        except Exception:
            self.stop();raise
    def stop(self):
        with self.lock:
            if self.started_at and not self.reported:
                self.reported=True
                logging.getLogger(__name__).info('Audio stats seconds=%.2f uplink_frames=%d downlink_frames=%d received_frames=%d capture_drops=%d playback_drops=%d max_receive_gap_ms=%.1f',
                    time.monotonic()-self.started_at,self.uplink_bytes//320,self.downlink_bytes//320,
                    self.received_frames,self.capture_drops,self.playback_drops,self.receive_gap_ms)
            was_active=self.active or self.media_started
            self.media_started=False
            self.active=False;self.stop_event.set()
            if self.client and self.client.profile.get('transport')=='ble':self.client.link.on_audio=None
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=.5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=.5)
            if self.route:self.route.close();self.route=None
            self.processes=[];threads,self.threads=self.threads,[]
        for thread in threads:
            if thread is not threading.current_thread():thread.join(timeout=.5)
        if was_active and not self.suppress_release and self.client and self.client.profile.get('transport')=='ble' and self.call:
            call=self.call
            def release():
                try:
                    link=self.client.link
                    with link.lock:current=dict(link.active) if link.active else None
                    if current and current['id']==call['id'] and current['generation']==call['generation']:
                        link.command('stop_media',call['id'],call['generation'])
                except Exception:pass
            threading.Thread(target=release,daemon=True).start()
    def close(self):
        self.closed=True;self.stop()


class AudioRouter:
    def __init__(self):
        self.engine=AudioSession();self.closed=False;self.generation=0;self.lock=threading.Lock()
    @property
    def active(self):return self.engine.active
    @property
    def error(self):return self.engine.error
    @error.setter
    def error(self,value):self.engine.error=value
    def start(self,client,call_id,*,early=False):
        if self.closed:raise AirSIMError('应用已退出')
        generation=self.generation
        self.engine.close()
        target=getattr(client,'current',client)
        if not getattr(target,'profile',None):raise AirSIMError('连接尚未就绪')
        with self.lock:
            if self.closed or generation!=self.generation:return
            engine=EarlyAudio() if early else (PacketAudio() if target.profile.get('transport') == 'ble' else AudioSession())
            self.engine=engine
        engine.start(target,call_id)
    def stop(self):self.engine.stop()
    def stop_async(self):
        # Cancel an in-flight start immediately; do not block the UI on PipeWire
        # teardown or send stop_media ahead of the more urgent end command.
        with self.lock:
            self.generation+=1
            engine=self.engine;engine.closed=True;engine.active=False
            engine.stop_event.set();engine.suppress_release=True
        threading.Thread(target=engine.close,name='airsim-audio-stop',daemon=True).start()
    def close(self):self.closed=True;self.engine.close()

