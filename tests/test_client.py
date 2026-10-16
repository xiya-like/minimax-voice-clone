"""minimax_client.py 的离线单元测试。

通过打桩替换 HTTP 层（_open），**不联网、不消耗额度**。
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import minimax_client as mc  # noqa: E402


class FakeResponse:
    def __init__(self, payload, headers=None, raw=None):
        self._body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.headers = headers if headers is not None else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Recorder:
    """替换 _open，记录请求并返回预设响应。"""

    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append({"url": req.full_url, "data": req.data, "headers": req.headers})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def last_json(self):
        return json.loads(self.requests[-1]["data"].decode("utf-8"))

    def last_headers(self):
        return self.requests[-1]["headers"]


def ok(payload, headers=None):
    body = dict(payload)
    body.setdefault("base_resp", {"status_code": 0, "status_msg": "success"})
    return FakeResponse(body, headers=headers)


class TestErrorMapping(unittest.TestCase):
    def test_mapped_codes(self):
        cases = {
            1004: "API Key",
            1008: "余额",
            2038: "实名认证",
            1043: "text_validation",
            2013: "格式",
            1002: "限流",
        }
        for code, keyword in cases.items():
            resp = FakeResponse({"base_resp": {"status_code": code, "status_msg": "raw"}})
            with mock.patch.object(mc, "_open", Recorder(resp)):
                with self.assertRaises(mc.MinimaxError) as ctx:
                    mc.voice_clone(api_key="k", payload={"file_id": 1, "voice_id": "MyVoice_001"})
            self.assertEqual(ctx.exception.code, code)
            self.assertIn(keyword, ctx.exception.message)

    def test_raw_status_msg_preserved(self):
        resp = FakeResponse({"base_resp": {"status_code": 9999, "status_msg": "something odd"}})
        with mock.patch.object(mc, "_open", Recorder(resp)):
            with self.assertRaises(mc.MinimaxError) as ctx:
                mc.voice_clone(api_key="k", payload={"file_id": 1, "voice_id": "MyVoice_001"})
        self.assertIn("9999", ctx.exception.message)
        self.assertEqual(ctx.exception.raw, "something odd")

    def test_trace_id_extracted(self):
        resp = ok({"trace_id": "abc"}, headers={"X-Trace-Id": "trace-xyz"})
        rec = Recorder(resp)
        with mock.patch.object(mc, "_open", rec):
            out = mc.voice_clone(api_key="k", payload={"file_id": 1, "voice_id": "MyVoice_001"})
        self.assertEqual(out["trace_id"], "trace-xyz")

    def test_network_error_not_retried(self):
        from urllib.error import URLError

        rec = Recorder(URLError("boom"))
        with mock.patch.object(mc, "_open", rec):
            with self.assertRaises(mc.MinimaxError) as ctx:
                mc.voice_clone(api_key="k", payload={"file_id": 1, "voice_id": "MyVoice_001"})
        self.assertEqual(ctx.exception.code, -2)
        self.assertEqual(len(rec.requests), 1, "失败后不得自动重试")

    def test_non_json_response(self):
        rec = Recorder(FakeResponse(None, raw=b"<html>oops</html>"))
        with mock.patch.object(mc, "_open", rec):
            with self.assertRaises(mc.MinimaxError) as ctx:
                mc.voice_clone(api_key="k", payload={"file_id": 1, "voice_id": "MyVoice_001"})
        self.assertEqual(ctx.exception.code, -4)
        self.assertEqual(len(rec.requests), 1)


class TestClonePayload(unittest.TestCase):
    def test_without_demo_text_omits_text_and_model(self):
        p = mc.build_clone_payload(file_id=123, voice_id="MyVoice_001")
        self.assertNotIn("text", p)
        self.assertNotIn("model", p, "不传试听文本时不得带 model，否则会触发合成计费")
        self.assertEqual(p["file_id"], 123)
        self.assertEqual(p["voice_id"], "MyVoice_001")

    def test_with_demo_text_always_sets_model(self):
        p = mc.build_clone_payload(file_id=1, voice_id="MyVoice_001", demo_text="你好")
        self.assertEqual(p["text"], "你好")
        self.assertEqual(p["model"], "speech-2.8-hd")

        p2 = mc.build_clone_payload(file_id=1, voice_id="MyVoice_001", demo_text="你好", model="speech-2.6-hd")
        self.assertEqual(p2["model"], "speech-2.6-hd")

    def test_empty_demo_text_treated_as_absent(self):
        p = mc.build_clone_payload(file_id=1, voice_id="MyVoice_001", demo_text="")
        self.assertNotIn("text", p)
        self.assertNotIn("model", p)

    def test_optional_flags_only_present_when_given(self):
        p = mc.build_clone_payload(file_id=1, voice_id="MyVoice_001")
        for key in ("need_noise_reduction", "need_volume_normalization", "aigc_watermark", "accuracy"):
            self.assertNotIn(key, p)

        p2 = mc.build_clone_payload(
            file_id=1,
            voice_id="MyVoice_001",
            need_noise_reduction=False,
            need_volume_normalization=True,
            aigc_watermark=True,
        )
        self.assertIs(p2["need_noise_reduction"], False, "显式关闭也要如实传递")
        self.assertIs(p2["need_volume_normalization"], True)
        self.assertIs(p2["aigc_watermark"], True)

    def test_clone_prompt_structure(self):
        p = mc.build_clone_payload(file_id=1, voice_id="MyVoice_001", prompt_file_id=999, prompt_text="你好。")
        self.assertEqual(p["clone_prompt"], {"prompt_audio": 999, "prompt_text": "你好。"})

    def test_japanese_language_boost_passthrough(self):
        p = mc.build_clone_payload(file_id=1, voice_id="MyVoice_001", language_boost="Japanese")
        self.assertEqual(p["language_boost"], "Japanese")

    def test_text_validation_and_accuracy(self):
        p = mc.build_clone_payload(
            file_id=1, voice_id="MyVoice_001", text_validation="こんにちは", accuracy=0.8
        )
        self.assertEqual(p["text_validation"], "こんにちは")
        self.assertEqual(p["accuracy"], 0.8)

    def test_wire_payload_reaches_server_intact(self):
        resp = ok({"demo_audio": ""})
        rec = Recorder(resp)
        with mock.patch.object(mc, "_open", rec):
            mc.voice_clone(
                api_key="k",
                payload=mc.build_clone_payload(file_id=7, voice_id="MyVoice_001", language_boost="Japanese"),
            )
        sent = rec.last_json()
        self.assertEqual(sent["language_boost"], "Japanese")
        self.assertNotIn("text", sent)


class TestCloneResponseHandling(unittest.TestCase):
    def test_empty_demo_audio_does_not_crash(self):
        rec = Recorder(ok({"demo_audio": "", "extra_info": {}}))
        with mock.patch.object(mc, "_open", rec):
            out = mc.voice_clone(api_key="k", payload={"file_id": 1, "voice_id": "MyVoice_001"})
        self.assertEqual(out["demo_audio"], "")
        self.assertEqual(out["voice_id"], "MyVoice_001")

    def test_sensitive_flag_surfaced(self):
        rec = Recorder(ok({"demo_audio": "", "input_sensitive_type": 5}))
        with mock.patch.object(mc, "_open", rec):
            out = mc.voice_clone(api_key="k", payload={"file_id": 1, "voice_id": "MyVoice_001"})
        self.assertEqual(out["input_sensitive_type"], 5)
        self.assertEqual(out["input_sensitive_label"], "谩骂")

    def test_usage_characters_returned(self):
        rec = Recorder(ok({"demo_audio": "http://x/a.mp3", "extra_info": {"usage_characters": 18}}))
        with mock.patch.object(mc, "_open", rec):
            out = mc.voice_clone(api_key="k", payload={"file_id": 1, "voice_id": "MyVoice_001"})
        self.assertEqual(out["extra_info"]["usage_characters"], 18)


class TestUpload(unittest.TestCase):
    def test_multipart_body_contains_purpose_and_file(self):
        rec = Recorder(ok({"file": {"file_id": 555, "bytes": 3, "filename": "a.wav"}}))
        with mock.patch.object(mc, "_open", rec):
            out = mc.upload_file(api_key="k", file_bytes=b"abc", filename="a.wav", purpose="voice_clone")
        self.assertEqual(out["file_id"], 555)
        headers = rec.last_headers()
        self.assertTrue(headers["Content-type"].startswith("multipart/form-data; boundary="))
        body = rec.requests[-1]["data"]
        self.assertIsInstance(body, bytes)
        self.assertIn(b'name="purpose"', body)
        self.assertIn(b"voice_clone", body)
        self.assertIn(b'abc', body)

    def test_missing_file_id_raises(self):
        rec = Recorder(ok({"file": {}}))
        with mock.patch.object(mc, "_open", rec):
            with self.assertRaises(mc.MinimaxError):
                mc.upload_file(api_key="k", file_bytes=b"abc", filename="a.wav", purpose="voice_clone")


class TestTextToSpeech(unittest.TestCase):
    def test_hex_decoding(self):
        payload = b"ID3fake-audio-bytes"
        resp = ok(
            {
                "data": {"audio": payload.hex(), "status": 2},
                "extra_info": {"usage_characters": 26, "audio_size": len(payload)},
            }
        )
        with mock.patch.object(mc, "_open", Recorder(resp)):
            out = mc.text_to_speech(
                api_key="k", payload=mc.build_tts_payload(voice_id="MyVoice_001", text="你好")
            )
        self.assertEqual(out["audio"], payload)
        self.assertEqual(out["extra_info"]["usage_characters"], 26)

    def test_size_mismatch_detected(self):
        payload = b"abc"
        resp = ok(
            {"data": {"audio": payload.hex()}, "extra_info": {"audio_size": 999}}
        )
        with mock.patch.object(mc, "_open", Recorder(resp)):
            with self.assertRaises(mc.MinimaxError) as ctx:
                mc.text_to_speech(api_key="k", payload=mc.build_tts_payload(voice_id="MyVoice_001", text="x"))
        self.assertEqual(ctx.exception.code, -8)

    def test_empty_audio_raises(self):
        resp = ok({"data": {"audio": ""}, "extra_info": {}})
        with mock.patch.object(mc, "_open", Recorder(resp)):
            with self.assertRaises(mc.MinimaxError) as ctx:
                mc.text_to_speech(api_key="k", payload=mc.build_tts_payload(voice_id="MyVoice_001", text="x"))
        self.assertEqual(ctx.exception.code, -6)

    def test_bad_hex_raises(self):
        resp = ok({"data": {"audio": "zzzznot-hex"}, "extra_info": {}})
        with mock.patch.object(mc, "_open", Recorder(resp)):
            with self.assertRaises(mc.MinimaxError) as ctx:
                mc.text_to_speech(api_key="k", payload=mc.build_tts_payload(voice_id="MyVoice_001", text="x"))
        self.assertEqual(ctx.exception.code, -7)

    def test_tts_payload_defaults(self):
        p = mc.build_tts_payload(voice_id="MyVoice_001", text="こんにちは")
        self.assertIs(p["stream"], False)
        self.assertEqual(p["output_format"], "hex")
        self.assertEqual(p["model"], "speech-2.8-hd")
        self.assertEqual(p["voice_setting"]["voice_id"], "MyVoice_001")

    def test_pronunciation_rules_for_japanese(self):
        p = mc.build_tts_payload(
            voice_id="MyVoice_001", text="20日", pronunciation_rules=["20日/はつか"]
        )
        self.assertEqual(p["pronunciation_dict"], {"tone": ["20日/はつか"]})


class TestKeyHandling(unittest.TestCase):
    def test_key_hint_masks(self):
        self.assertEqual(mc.key_hint("abcdef123456"), "****3456")
        self.assertEqual(mc.key_hint("abc"), "****")
        self.assertEqual(mc.key_hint(""), "")

    def test_load_api_key_variants(self):
        import tempfile

        cases = {
            "plain": "MINIMAX_API_KEY=sk-abc123\n",
            "quoted": 'MINIMAX_API_KEY="sk-abc123"\n',
            "exported": "export MINIMAX_API_KEY=sk-abc123\n",
            "spaced": "  MINIMAX_API_KEY = sk-abc123  \n",
            "commented": "# comment\nMINIMAX_API_KEY=sk-abc123\n",
        }
        for label, content in cases.items():
            with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as fh:
                fh.write(content)
                path = fh.name
            try:
                self.assertEqual(mc.load_api_key(path), "sk-abc123", label)
            finally:
                os.remove(path)

    def test_missing_env_returns_empty(self):
        self.assertEqual(mc.load_api_key("/nonexistent/path/.env"), "")

    def test_error_message_never_leaks_key(self):
        resp = FakeResponse({"base_resp": {"status_code": 1004, "status_msg": "auth failed"}})
        with mock.patch.object(mc, "_open", Recorder(resp)):
            with self.assertRaises(mc.MinimaxError) as ctx:
                mc.voice_clone(api_key="sk-supersecret", payload={"file_id": 1, "voice_id": "MyVoice_001"})
        blob = ctx.exception.message + str(ctx.exception.raw) + str(ctx.exception.to_dict())
        self.assertNotIn("supersecret", blob)


if __name__ == "__main__":
    unittest.main()
