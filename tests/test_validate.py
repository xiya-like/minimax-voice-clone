"""validate.py 的离线单元测试。不联网、不消耗额度。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import validate as vd  # noqa: E402


def make_wav(seconds: float, sample_rate: int = 44100, channels: int = 1, bits: int = 16) -> bytes:
    """构造一个只有头部的合法 WAV，用于测试时长解析。"""
    byte_rate = sample_rate * channels * bits // 8
    data_size = int(byte_rate * seconds)
    block_align = channels * bits // 8
    fmt = (
        b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + bits.to_bytes(2, "little")
    )
    data = b"\x00" * 64 + b"\x00" * max(0, data_size - 64)
    body = b"WAVE" + fmt + b"data" + len(data).to_bytes(4, "little") + data
    return b"RIFF" + len(body).to_bytes(4, "little") + body


class TestVoiceId(unittest.TestCase):
    def test_accepts_valid(self):
        for vid in ("MyVoice_001", "abcdefgh", "A1_b2-c3d4", "x" * 256):
            ok, _ = vd.validate_voice_id(vid)
            self.assertTrue(ok, f"{vid} 应通过")

    def test_too_short(self):
        ok, msg = vd.validate_voice_id("abc1234")
        self.assertFalse(ok)
        self.assertIn("长度", msg)

    def test_too_long(self):
        ok, _ = vd.validate_voice_id("a" * 257)
        self.assertFalse(ok)

    def test_first_char_must_be_letter(self):
        for vid in ("1abcdefgh", "_abcdefgh", "-abcdefgh"):
            ok, msg = vd.validate_voice_id(vid)
            self.assertFalse(ok)
            self.assertIn("首字符", msg)

    def test_illegal_characters(self):
        ok, msg = vd.validate_voice_id("abcdefgh!")
        self.assertFalse(ok)
        self.assertIn("非法字符", msg)

    def test_last_char_cannot_be_dash_or_underscore(self):
        for vid in ("abcdefg-", "abcdefg_"):
            ok, msg = vd.validate_voice_id(vid)
            self.assertFalse(ok)
            self.assertIn("末位", msg)

    def test_empty(self):
        ok, _ = vd.validate_voice_id("")
        self.assertFalse(ok)


class TestFileValidation(unittest.TestCase):
    def test_allowed_extensions(self):
        for name in ("a.mp3", "b.m4a", "c.wav", "D.WAV"):
            ok, _ = vd.validate_audio(name, 1024, "voice_clone", 30.0)
            self.assertTrue(ok, name)

    def test_rejected_extensions(self):
        for name in ("a.mp4", "b.txt", "c.webm", "noext"):
            ok, msg = vd.validate_audio(name, 1024, "voice_clone", 30.0)
            self.assertFalse(ok, name)
            self.assertIn("格式", msg)

    def test_size_boundary(self):
        under = vd.MAX_BYTES - 1000
        over = vd.MAX_BYTES + 1000
        self.assertTrue(vd.validate_audio("a.mp3", under, "voice_clone", 30.0)[0])
        ok, msg = vd.validate_audio("a.mp3", over, "voice_clone", 30.0)
        self.assertFalse(ok)
        self.assertIn("20 MB", msg)

    def test_duration_bounds_for_clone(self):
        ok, msg = vd.validate_audio("a.wav", 1024, "voice_clone", 9.9)
        self.assertFalse(ok)
        self.assertIn("10 秒", msg)
        self.assertTrue(vd.validate_audio("a.wav", 1024, "voice_clone", 10.0)[0])
        self.assertTrue(vd.validate_audio("a.wav", 1024, "voice_clone", 300.0)[0])
        ok, msg = vd.validate_audio("a.wav", 1024, "voice_clone", 300.1)
        self.assertFalse(ok)
        self.assertIn("5 分钟", msg)

    def test_prompt_must_be_under_8s(self):
        self.assertTrue(vd.validate_audio("a.wav", 1024, "prompt_audio", 7.9)[0])
        ok, msg = vd.validate_audio("a.wav", 1024, "prompt_audio", 8.0)
        self.assertFalse(ok)
        self.assertIn("8 秒", msg)

    def test_unknown_purpose(self):
        ok, _ = vd.validate_audio("a.mp3", 1024, "bogus", 30.0)
        self.assertFalse(ok)

    def test_empty_file(self):
        ok, _ = vd.validate_audio("a.mp3", 0, "voice_clone", 30.0)
        self.assertFalse(ok)

    def test_unknown_duration_is_not_rejected(self):
        """时长无法测量时应放行，交由服务端校验，而不是假装已校验。"""
        ok, msg = vd.validate_audio("a.mp3", 1024, "voice_clone", None)
        self.assertTrue(ok)
        self.assertIn("未能本地测量", msg)


class TestWavDuration(unittest.TestCase):
    def test_mono_44100(self):
        d = vd.parse_wav_duration(make_wav(3.0))
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d, 3.0, delta=0.01)

    def test_stereo(self):
        d = vd.parse_wav_duration(make_wav(2.5, sample_rate=48000, channels=2))
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d, 2.5, delta=0.01)

    def test_rejects_non_wav(self):
        self.assertIsNone(vd.parse_wav_duration(b"ID3\x04\x00" + b"\x00" * 100))

    def test_rejects_truncated(self):
        self.assertIsNone(vd.parse_wav_duration(b"RIFF"))

    def test_roundtrip_through_validate(self):
        raw = make_wav(30.0)
        d = vd.parse_wav_duration(raw)
        ok, msg = vd.validate_audio("sample.wav", len(raw), "voice_clone", d)
        self.assertTrue(ok, msg)


class TestProbeDuration(unittest.TestCase):
    def test_probe_returns_none_or_float(self):
        """ffprobe 可能不存在；无论哪种情况都不应抛异常。"""
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
            fh.write(b"\x00" * 128)
            path = fh.name
        try:
            result = vd.probe_duration(path)
            self.assertTrue(result is None or isinstance(result, float))
        finally:
            os.remove(path)


class TestChargedCharacters(unittest.TestCase):
    def test_han_counts_as_two(self):
        # 官方口径：1 个汉字 = 2 个字符
        self.assertEqual(vd.count_charged_characters("你好"), 4)
        self.assertEqual(vd.count_charged_characters("你好，世界"), 9)  # 4 汉字×2 + 逗号×1

    def test_letters_count_as_one(self):
        self.assertEqual(vd.count_charged_characters("hello"), 5)

    def test_punctuation_and_space_count_as_one(self):
        self.assertEqual(vd.count_charged_characters("，。！？ "), 5)

    def test_kana_counts_as_two(self):
        self.assertEqual(vd.count_charged_characters("こんにちは"), 10)
        self.assertEqual(vd.count_charged_characters("カタカナ"), 8)

    def test_mixed(self):
        # 你好(4) + ，(1) + world(5) + 空格(1) + 123(3) + ！(1)
        self.assertEqual(vd.count_charged_characters("你好，world 123！"), 15)

    def test_empty(self):
        self.assertEqual(vd.count_charged_characters(""), 0)


if __name__ == "__main__":
    unittest.main()
