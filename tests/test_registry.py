"""server.py 中音色台账的离线单元测试。不联网、不消耗额度。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv  # noqa: E402


class VoiceRegistryTest(unittest.TestCase):
    """台账是唯一的状态真相源，持久化行为必须可靠。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "voices.json")
        self.patch = mock.patch.object(srv, "VOICES_PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _read(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_create_persists(self):
        srv.upsert_voice("CliteRoom_Suzune")
        self.assertTrue(os.path.isfile(self.path))
        data = self._read()
        self.assertEqual(len(data["voices"]), 1)
        self.assertEqual(data["voices"][0]["voice_id"], "CliteRoom_Suzune")
        self.assertEqual(data["voices"][0]["uses"], 0)
        self.assertIsNone(data["voices"][0]["last_used_at"])

    def test_increment_uses_is_persisted(self):
        """曾经的 bug：更新已有记录时在写盘前就 return，次数永远停在 0。"""
        srv.upsert_voice("CliteRoom_Suzune")
        srv.upsert_voice("CliteRoom_Suzune", last_used=True)
        srv.upsert_voice("CliteRoom_Suzune", last_used=True)

        data = self._read()
        self.assertEqual(len(data["voices"]), 1, "不应产生重复记录")
        self.assertEqual(data["voices"][0]["uses"], 2)
        self.assertIsNotNone(data["voices"][0]["last_used_at"])

    def test_no_duplicate_records(self):
        for _ in range(5):
            srv.upsert_voice("VoiceABC_01", last_used=True)
        self.assertEqual(len(self._read()["voices"]), 1)

    def test_voice_exists(self):
        self.assertFalse(srv.voice_exists("Nope_12345"))
        srv.upsert_voice("VoiceABC_01")
        self.assertTrue(srv.voice_exists("VoiceABC_01"))

    def test_countdown(self):
        srv.upsert_voice("VoiceABC_01")
        rows = srv.voices_with_countdown()
        self.assertEqual(rows[0]["days_left"], srv.VOICE_TTL_DAYS)
        self.assertFalse(rows[0]["expired"])

    def test_expired_voice_flagged(self):
        srv.upsert_voice("VoiceABC_01")
        data = self._read()
        data["voices"][0]["expires_at"] = "2020-01-01T00:00:00+00:00"
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        rows = srv.voices_with_countdown()
        self.assertTrue(rows[0]["expired"])
        self.assertEqual(rows[0]["days_left"], 0, "已过期应显示 0 天，而不是负数")

    def test_corrupt_ledger_is_backed_up_not_lost(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        result = srv.load_voices()
        self.assertEqual(result["voices"], [])
        self.assertTrue(os.path.isfile(self.path + ".bak"), "损坏文件应被备份而不是静默丢弃")

    def test_wrong_structure_treated_as_corrupt(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"voices": "not-a-list"}, fh)
        self.assertEqual(srv.load_voices()["voices"], [])
        self.assertTrue(os.path.isfile(self.path + ".bak"))


class PathSafetyTest(unittest.TestCase):
    """音频落盘文件名由服务端生成，读取时必须限制在 output 目录内。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "output")
        os.makedirs(self.out)
        self.patch = mock.patch.object(srv, "OUTPUT_DIR", self.out)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_valid_name_resolves(self):
        with open(os.path.join(self.out, "a.mp3"), "wb") as fh:
            fh.write(b"x")
        self.assertIsNotNone(srv.resolve_audio("a.mp3"))

    def test_traversal_rejected(self):
        for name in ("../server.py", "..\\server.py", "a/../../b", "/etc/passwd"):
            self.assertIsNone(srv.resolve_audio(name), name)

    def test_hidden_and_missing_rejected(self):
        self.assertIsNone(srv.resolve_audio(".hidden"))
        self.assertIsNone(srv.resolve_audio("nope.mp3"))
        self.assertIsNone(srv.resolve_audio(""))

    def test_generated_name_contains_no_user_input(self):
        name = srv.save_audio("../../evil_id", "demo", b"data")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertTrue(os.path.isfile(os.path.join(self.out, name)))


if __name__ == "__main__":
    unittest.main()
