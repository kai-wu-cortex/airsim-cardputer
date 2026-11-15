"""Read-only event-driven LAN inbox. Never ACK the shared phone queue."""
import threading


class SMSFeed:
    def __init__(self,client):
        self.client=client
        self.stopped=threading.Event()
        self.lock=threading.Lock()
        self.pending={}
        self.thread=threading.Thread(target=self.run,name='airsim-sms-events',daemon=True)
        self.thread.start()

    def fetch(self):
        from .protocol import AirSIMError
        messages,_=self.client.signed('GET','/api/sms')
        if not isinstance(messages,list) or not all(isinstance(m,dict) for m in messages):
            raise AirSIMError('短信数据格式无效')
        with self.lock:
            for message in messages:
                identity=message.get('delivery_id') or '\0'.join(str(message.get(k,'')) for k in ('sender','timestamp','content'))
                self.pending[identity]=message
            self.pending=dict(list(self.pending.items())[-512:])

    def run(self):
        revision=0
        while not self.stopped.is_set():
            try:
                self.client.health()
                if self.stopped.is_set():break
                self.fetch()
                while not self.stopped.is_set():
                    event,_=self.client.signed('GET',f'/api/events?after={revision}&timeout_ms=4000')
                    if self.stopped.is_set():break
                    if not isinstance(event,dict):raise ValueError('Invalid event')
                    current=event.get('revision')
                    if not isinstance(current,int) or isinstance(current,bool) or current<0:
                        raise ValueError('Invalid event revision')
                    # Fetch immediately; do not wait for calls/status or the UI worker.
                    if current!=revision or event.get('sms_pending',0):self.fetch()
                    revision=current
                    # Also bounds traffic if a peer ignores the long-poll timeout.
                    if self.stopped.wait(.05):break
            except Exception as error:
                # Older Agents retain normal snapshot polling when events are absent.
                if getattr(error,'status',None)==404:break
                if self.stopped.wait(5):break

    def drain(self):
        with self.lock:
            messages=list(self.pending.values());self.pending.clear()
        return messages

    def close(self):
        self.stopped.set()
        # In-flight read is bounded by the transport timeout; never stall hangup/UI.
        self.thread.join(timeout=.1)
