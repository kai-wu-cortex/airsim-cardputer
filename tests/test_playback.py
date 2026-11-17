import math
import struct
import unittest

from airsim_cp.playback import SpeechPlayback


def pcm(samples):
    return struct.pack('<' + 'h' * len(samples), *samples)


def samples(data):
    return struct.unpack('<' + 'h' * (len(data) // 2), data)


class SpeechPlaybackTests(unittest.TestCase):
    def test_quiet_speech_gains_six_db_without_extra_frames(self):
        source = [round(3000 * math.sin(i * .3)) for i in range(160)]
        output = samples(SpeechPlayback(gain_db=6).process(pcm(source)))
        ratio = math.sqrt(sum(x*x for x in output) / sum(x*x for x in source))
        self.assertAlmostEqual(20 * math.log10(ratio), 6, places=2)
        self.assertEqual(len(output), len(source))

    def test_full_scale_input_is_bounded_and_retains_polarity(self):
        output = samples(SpeechPlayback().process(pcm([-32768, 32767, 0] * 50)))
        self.assertLessEqual(max(abs(x) for x in output), 31128)
        self.assertLess(output[0], 0)
        self.assertGreater(output[1], 0)
        self.assertEqual(output[2], 0)

    def test_limiter_releases_gradually_after_a_loud_frame(self):
        processor = SpeechPlayback(gain_db=6)
        processor.process(pcm([32767] * 160))
        quiet = pcm([1000] * 160)
        levels = [samples(processor.process(quiet))[0] for _ in range(60)]
        self.assertLess(levels[0], 1100)
        self.assertGreater(levels[-1], 1950)
        self.assertEqual(levels, sorted(levels))

    def test_playback_and_post_aec_microphone_gain(self):
        self.assertAlmostEqual(samples(SpeechPlayback().process(pcm([1000])))[0], 3981, delta=1)
        self.assertAlmostEqual(samples(SpeechPlayback(gain_db=9).process(pcm([1000])))[0], 2818, delta=1)

    def test_silence_stays_silent_and_invalid_samples_are_rejected(self):
        processor = SpeechPlayback()
        self.assertEqual(processor.process(bytes(320)), bytes(320))
        self.assertEqual(processor.process(b''), b'')
        with self.assertRaises(ValueError):
            processor.process(b'\0')
