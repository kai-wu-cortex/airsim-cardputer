"""Framebuffer and kernel-translated evdev input; no custom sticky modifier logic."""
import fcntl
import mmap
from pathlib import Path
import struct
from PIL import Image, ImageChops


def rgb565(image):
    """Pack RGB565 in native loops instead of iterating 54,400 pixels in Python."""
    red,green,blue=image.convert('RGB').split()
    low=ImageChops.add(green.point([(x & 28) << 3 for x in range(256)]),
                       blue.point([x >> 3 for x in range(256)]))
    high=ImageChops.add(red.point([x & 248 for x in range(256)]),
                        green.point([x >> 5 for x in range(256)]))
    return Image.merge('LA',(low,high)).tobytes()


class Framebuffer:
    def __init__(self, path='/dev/fb0'):
        self.file = open(path, 'r+b', buffering=0)
        var, fix = bytearray(160), bytearray(80)
        fcntl.ioctl(self.file, 0x4600, var, True)
        fcntl.ioctl(self.file, 0x4602, fix, True)
        values = struct.unpack('40I', var)
        self.width, self.height, _, virtual_h, self.xoff, self.yoff, depth = values[:7]
        self.stride = struct.unpack_from('I', fix, 48)[0]
        if (self.width, self.height, depth) != (320, 170, 16) or values[8:13] != (11, 5, 0, 5, 6):
            raise RuntimeError('Requires Cardputer 320×170 RGB565 framebuffer')
        self.memory = mmap.mmap(self.file.fileno(), self.stride * virtual_h)

    def show(self, image):
        packed = rgb565(image)
        for y in range(self.height):
            offset = (y + self.yoff) * self.stride + self.xoff * 2
            self.memory[offset:offset + 640] = packed[y * 640:(y + 1) * 640]

    def close(self):
        self.memory.close(); self.file.close()


def keyboard():
    from evdev import InputDevice
    for event in Path('/sys/class/input').glob('event*'):
        if (event / 'device/name').read_text().strip() == 'tca8418c':
            return InputDevice('/dev/input/' + event.name)
    raise RuntimeError('Cardputer keyboard not found')


# The OS driver handles fn/sym/shift latch state; only ordinary US key translation is done here.
KEY_CHARS = dict(zip(range(2, 12), '1234567890'))
KEY_CHARS.update(dict(zip(range(16, 26), 'qwertyuiop')))
KEY_CHARS.update(dict(zip(range(30, 39), 'asdfghjkl')))
KEY_CHARS.update(dict(zip(range(44, 51), 'zxcvbnm')))
KEY_CHARS.update({12:'-', 13:'=', 26:'[', 27:']', 39:';', 40:"'", 41:'`', 43:'\\', 51:',', 52:'.', 53:'/', 57:' '})
SHIFT = dict(zip('1234567890-=[];\'`,./\\', '!@#$%^&*()_+{}:"~<>?|'))


def character(code, shifted):
    char = KEY_CHARS.get(code, '')
    return SHIFT.get(char, char.upper()) if shifted else char
