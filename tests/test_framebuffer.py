import random
import struct
import unittest
from PIL import Image
from airsim_cp.hardware import rgb565,Framebuffer


class FramebufferTests(unittest.TestCase):
    def test_rgb565_matches_reference_including_channel_boundaries(self):
        rng=random.Random(42)
        colors=[(r,g,b) for r in (0,7,8,127,248,255) for g in range(256) for b in (0,7,8,248,255)]
        colors += [tuple(rng.randrange(256) for _ in range(3)) for _ in range(4000)]
        image=Image.new('RGB',(len(colors),1));image.putdata(colors)
        expected=b''.join(struct.pack('<H',((r>>3)<<11)|((g>>2)<<5)|(b>>3)) for r,g,b in colors)
        self.assertEqual(rgb565(image),expected)

    def test_stride_and_offsets_preserve_unrelated_framebuffer_bytes(self):
        fb=object.__new__(Framebuffer);fb.width=320;fb.height=170
        fb.stride=660;fb.xoff=3;fb.yoff=1
        fb.memory=bytearray([91])*(fb.stride*172)
        image=Image.new('RGB',(320,170),(255,0,0));fb.show(image)
        for y in range(172):
            row=fb.memory[y*660:(y+1)*660]
            if 1<=y<171:
                self.assertEqual(row[:6],bytes([91])*6)
                self.assertEqual(row[6:646],b'\x00\xf8'*320)
                self.assertEqual(row[646:],bytes([91])*14)
            else:self.assertEqual(row,bytes([91])*660)
