"""Bounded, cancellable LAN discovery; advertisements are hints, never credentials."""
import socket
import threading
import time
from .protocol import AirSIMError, valid_host, valid_port

PAIR = '_airsim-pair._tcp.local.'
GATEWAY = '_airsim-vowlan._tcp.local.'


def endpoints(info, service_type):
    if service_type not in (PAIR, GATEWAY):
        return []
    props = {k.decode('utf-8', 'replace'): (v or b'').decode('utf-8', 'replace')
             for k, v in info.properties.items()}
    if props.get('v') != '1':
        return []
    try:
        port = valid_port(info.port)
        if service_type == PAIR and int(props.get('expires', '0')) <= time.time() * 1000:
            return []
        pcm = valid_port(props.get('pcm_port')) if service_type == GATEWAY else None
    except (AirSIMError, ValueError):
        return []
    result = []
    for address in info.addresses:
        try:
            host = valid_host(socket.inet_ntoa(address))
        except (AirSIMError, OSError):
            continue
        name = props.get('device_name') or info.name.removesuffix('.' + service_type)
        name = ''.join(c for c in name if c.isprintable())[:80] or 'AirSIM'
        result.append(dict(name=name, host=host, port=port, pcm_port=pcm,
                           agent_id=props.get('agent_id', ''),
                           kind='pair' if service_type == PAIR else 'gateway',
                           expires=int(props.get('expires', '0')) if service_type == PAIR else 0))
    return result


def group_devices(rows):
    """Join a unique pairing advertisement to its gateway on the same address.

    This is a navigation hint only; code-based pairing still authenticates the phone.
    Ambiguous same-host pairing sessions remain separate selectable entries.
    """
    pairs=[d for d in rows if d['kind']=='pair']
    result=[];joined=set()
    for device in rows:
        if device['kind']!='gateway':continue
        matches=[p for p in pairs if p['host']==device['host']]
        if len(matches)==1:
            device=dict(device,pairing=matches[0])
            joined.add((matches[0]['host'],matches[0]['port']))
        result.append(device)
    result.extend(p for p in pairs if (p['host'],p['port']) not in joined)
    return sorted(result,key=lambda d:(d['name'],d['host'],d['port']))


class LANDiscovery:
    def __init__(self, duration=8):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.records = {}
        self.done = False
        self.error = ''
        self.duration = duration
        self.thread = threading.Thread(target=self.run, name='airsim-discovery', daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            now = time.time() * 1000
            unique = {(d['kind'], d['host'], d['port']): d for rows in self.records.values()
                      for d in rows if not d['expires'] or d['expires'] > now}
            return group_devices(list(unique.values())), self.done, self.error

    def close(self):
        self.cancelled.set()

    def add_service(self, zc, service_type, name):
        if self.cancelled.is_set():
            return
        info = zc.get_service_info(service_type, name, timeout=600)
        if info and not self.cancelled.is_set():
            rows = endpoints(info, service_type)
            with self.lock:
                self.records[(service_type, name)] = rows

    update_service = add_service

    def remove_service(self, zc, service_type, name):
        with self.lock:
            self.records.pop((service_type, name), None)

    def run(self):
        zc = browser = None
        try:
            from zeroconf import Zeroconf, ServiceBrowser, IPVersion
            zc = Zeroconf(ip_version=IPVersion.V4Only)
            browser = ServiceBrowser(zc, [PAIR, GATEWAY], listener=self)
            self.cancelled.wait(self.duration)
        except Exception:
            with self.lock:
                self.error = '自动发现暂不可用，请重扫或手动连接'
        finally:
            if browser is not None:
                browser.cancel()
            if zc is not None:
                zc.close()
            with self.lock:
                self.done = True
