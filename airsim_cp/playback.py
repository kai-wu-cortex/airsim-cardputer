"""Shared call playback policy: system volume plus bounded speech gain."""
import math
import struct


def playback_command():
    # Use the default output, with no remembered per-app attenuation. Hardware
    # volume and mute remain owned by the OS; never change the sink here.
    return ['pw-cat', '--playback', '--raw', '--format', 's16', '--rate', '8000',
            '--channels', '1', '--latency', '40ms', '--volume', '1',
            '--media-role', 'Communication', '--properties',
            '{ application.name = "AirSIM Cardputer" '
            'node.name = "airsim-cardputer-playback" state.restore-props = false }', '-']


class SpeechPlayback:
    """Configurable gain with frame peak limiting and a 300 ms release, no extra queue.

    Each input is an existing 20 ms S16LE frame. Inspecting its peak before
    scaling avoids clipping without collecting any additional audio frames.
    """
    def __init__(self, gain_db=12):
        self.target = 10 ** (gain_db / 20)
        self.gain = self.target

    def process(self, pcm):
        if not pcm:
            return pcm
        if len(pcm) % 2:
            raise ValueError('S16LE audio requires complete samples')
        samples = struct.unpack('<' + 'h' * (len(pcm) // 2), pcm)
        peak = max(abs(sample) for sample in samples)
        ceiling = min(self.target, 31128 / peak) if peak else self.target
        if ceiling < self.gain:
            self.gain = ceiling
        else:
            release = 1 - math.exp(-len(samples) / (8000 * .3))
            self.gain += (ceiling - self.gain) * release
        return struct.pack('<' + 'h' * len(samples),
                           *(round(sample * self.gain) for sample in samples))
