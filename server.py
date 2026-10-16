"""MiniMax 音色克隆本地网页工具 —— 服务入口。

仅监听 127.0.0.1；API Key 只在服务端读取；任何失败都不自动重试。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import minimax_client as mc
import validate as vd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
VOICES_PATH = os.path.join(DATA_DIR, "voices.json")

# 官方规则：复刻音色 7 天内未正式调用会被删除
VOICE_TTL_DAYS = 7

MAX_UPLOAD_BYTES = vd.MAX_BYTES

_voices_lock = threading.Lock()


# ---------------------------------------------------------------- 启动自检


def ensure_dirs() -> None:
    for path in (DATA_DIR, OUTPUT_DIR, TMP_DIR):
        os.makedirs(path, exist_ok=True)
    probe = os.path.join(DATA_DIR, ".write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        raise SystemExit(f"[启动失败] data 目录不可写：{DATA_DIR}\n原因：{exc}")


def clear_tmp() -> None:
    try:
        for name in os.listdir(TMP_DIR):
            p = os.path.join(TMP_DIR, name)
            if os.path.isfile(p):
                os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------- 音色台账


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_voices() -> dict:
    """读取台账。损坏时备份并以空台账启动，不静默丢数据。"""
    if not os.path.isfile(VOICES_PATH):
        return {"voices": []}
    try:
        with open(VOICES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
            raise ValueError("结构不符合预期")
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        try:
            os.replace(VOICES_PATH, VOICES_PATH + ".bak")
            print(f"[警告] 台账文件损坏，已备份为 voices.json.bak，并以空台账启动")
        except OSError:
            pass
        return {"voices": []}


def save_voices(data: dict) -> None:
    tmp = VOICES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, VOICES_PATH)


def upsert_voice(voice_id: str, *, last_used: bool = False) -> dict:
    """写入或更新音色记录，返回该条记录。"""
    with _voices_lock:
        data = load_voices()
        now = _now()
        for item in data["voices"]:
            if item.get("voice_id") == voice_id:
                if last_used:
                    item["last_used_at"] = now.isoformat()
                    item["uses"] = int(item.get("uses", 0)) + 1
                # 更新已有记录时也必须落盘，否则使用次数会丢失
                save_voices(data)
                return item
        record = {
            "voice_id": voice_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=VOICE_TTL_DAYS)).isoformat(),
            "last_used_at": now.isoformat() if last_used else None,
            "uses": 1 if last_used else 0,
        }
        data["voices"].append(record)
        save_voices(data)
        return record


def voice_exists(voice_id: str) -> bool:
    with _voices_lock:
        data = load_voices()
        return any(v.get("voice_id") == voice_id for v in data["voices"])


def voices_with_countdown() -> list[dict]:
    now = _now()
    out = []
    for item in load_voices()["voices"]:
        record = dict(item)
        try:
            expires = datetime.fromisoformat(record.get("expires_at", ""))
            remaining = (expires - now).total_seconds()
            # 向上取整：刚创建的音色应显示 7 天，而不是因为不足整日截断成 6
            record["days_left"] = int(math.ceil(remaining / 86400)) if remaining > 0 else 0
            record["expired"] = remaining <= 0
        except (TypeError, ValueError):
            record["days_left"] = None
            record["expired"] = False
        out.append(record)
    return out


# ---------------------------------------------------------------- 文件落盘


def save_audio(voice_id: str, kind: str, data: bytes, extension: str = "mp3") -> str:
    """原子落盘。文件名由服务端生成，不使用用户提供的名称。"""
    safe = "".join(ch for ch in voice_id if ch.isalnum() or ch in "-_")[:64] or "voice"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{safe}_{stamp}_{kind}.{extension}"
    final = os.path.join(OUTPUT_DIR, filename)
    tmp = final + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, final)
    return filename


def resolve_audio(name: str) -> str | None:
    """把请求的文件名解析为 output 目录内的真实路径，越界返回 None。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = os.path.realpath(os.path.join(OUTPUT_DIR, name))
    root = os.path.realpath(OUTPUT_DIR)
    if os.path.dirname(candidate) != root:
        return None
    return candidate if os.path.isfile(candidate) else None


# ---------------------------------------------------------------- 请求处理


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniMaxVoiceTool/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 基础工具

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: str, download_name: str | None = None) -> None:
        ctype = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".flac": "audio/flac",
            ".pcm": "audio/L16",
            ".opus": "audio/ogg",
        }.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        with open(path, "rb") as fh:
            self._send_bytes(fh.read(), ctype, download_name)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 4 * 1024 * 1024:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _drain(self, length: int, cap: int = 128 * 1024 * 1024) -> bool:
        """丢弃已声明长度的请求体。

        超限时若直接回 400 而不读完 body，客户端会收到连接重置而不是可读的
        JSON 提示。这里分块丢弃以保证客户端能正常读到错误信息。
        """
        if length > cap:
            return False
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
        return True

    # ---------------------------------------------------------- 安全校验

    def _guard(self) -> bool:
        """Host / Origin 校验，阻断 DNS rebinding 与跨站调用。"""
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}
        if host not in allowed:
            self._send_json({"ok": False, "error": "非法 Host，已拒绝"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlparse(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost"):
                self._send_json({"ok": False, "error": "跨源请求，已拒绝"}, 403)
                return False
        return True

    # ---------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            index = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(index):
                self._send_json({"ok": False, "error": "缺少 web/index.html"}, 500)
                return
            with open(index, "rb") as fh:
                self._send_bytes(fh.read(), "text/html; charset=utf-8")
            return

        if route == "/api/health":
            key = mc.load_api_key()
            self._send_json(
                {
                    "ok": True,
                    "key_configured": bool(key),
                    "key_hint": mc.key_hint(key),
                    "endpoint": mc.BASE_URL,
                    "region": "cn",
                    "models": list(mc.T2A_MODELS),
                    "languages": list(mc.LANGUAGE_BOOST_OPTIONS),
                    "formats": list(mc.AUDIO_FORMATS),
                    "emotions": list(mc.EMOTIONS),
                    "ttl_days": VOICE_TTL_DAYS,
                }
            )
            return

        if route == "/api/voices":
            self._send_json({"ok": True, "voices": voices_with_countdown()})
            return

        if route.startswith("/audio/"):
            name = urllib.parse.unquote(route[len("/audio/") :])
            path = resolve_audio(name)
            if not path:
                self._send_json({"ok": False, "error": "文件不存在"}, 404)
                return
            self._send_file(path)
            return

        self._send_json({"ok": False, "error": "未知路径"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        try:
            if route == "/api/upload":
                self._handle_upload(parsed)
            elif route == "/api/clone":
                self._handle_clone()
            elif route == "/api/tts":
                self._handle_tts()
            else:
                self._send_json({"ok": False, "error": "未知路径"}, 404)
        except mc.MinimaxError as exc:
            self._send_json(exc.to_dict(), 200)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免静默失败
            self._send_json({"ok": False, "error": f"本地服务异常：{exc}"}, 500)

    # ---------------------------------------------------------- 处理器

    def _handle_upload(self, parsed) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        purpose = (query.get("purpose") or ["voice_clone"])[0]
        if purpose not in ("voice_clone", "prompt_audio"):
            self._send_json({"ok": False, "error": f"不支持的 purpose：{purpose}"}, 400)
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json({"ok": False, "error": "未收到文件内容"}, 400)
            return
        if length > MAX_UPLOAD_BYTES:
            # 读取前先拦下，不落盘、不产生外部调用；先丢弃请求体以便客户端读到提示
            drained = self._drain(length)
            if drained:
                self._send_json(
                    {
                        "ok": False,
                        "error": f"文件大小超过 20 MB 上限（收到 {length / 1048576:.1f} MB），已在本地拦下",
                        "checked_locally": True,
                    },
                    400,
                )
            else:
                self.close_connection = True
            return

        raw_name = self.headers.get("X-Filename") or "upload.bin"
        # 前端做了 URL 编码；解码后再只取纯文件名，丢弃任何路径成分
        decoded = urllib.parse.unquote(raw_name)
        safe_name = os.path.basename(decoded).replace('"', "").replace("\x00", "")[:120]
        ext = os.path.splitext(safe_name)[1].lower()

        data = self.rfile.read(length)
        # wav 直接解析文件头；其余格式或解析失败时回退到 ffprobe
        duration = vd.parse_wav_duration(data) if ext == ".wav" else None
        if duration is None:
            tmp_path = os.path.join(TMP_DIR, f"probe-{int(time.time() * 1000)}{ext}")
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            try:
                duration = vd.probe_duration(tmp_path)
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        ok, message = vd.validate_audio(safe_name, len(data), purpose, duration)
        if not ok:
            self._send_json({"ok": False, "error": message, "checked_locally": True}, 400)
            return

        key = mc.load_api_key()
        if not key:
            self._send_json({"ok": False, "error": "未配置 MINIMAX_API_KEY，请在 .env 中填写"}, 400)
            return

        result = mc.upload_file(
            api_key=key,
            file_bytes=data,
            filename=safe_name,
            purpose=purpose,
            timeout=60,
        )
        self._send_json(
            {
                "ok": True,
                "file_id": result["file_id"],
                "bytes": result["bytes"],
                "filename": result["filename"],
                "duration": duration,
                "duration_verified": duration is not None,
                "message": message,
                "trace_id": result["trace_id"],
            }
        )

    def _handle_clone(self) -> None:
        body = self._read_json()
        key = mc.load_api_key()
        if not key:
            self._send_json({"ok": False, "error": "未配置 MINIMAX_API_KEY，请在 .env 中填写"}, 400)
            return

        file_id = body.get("file_id")
        voice_id = (body.get("voice_id") or "").strip()
        if not file_id:
            self._send_json({"ok": False, "error": "请先上传待复刻音频"}, 400)
            return

        ok, message = vd.validate_voice_id(voice_id)
        if not ok:
            self._send_json({"ok": False, "error": f"voice_id 不合规：{message}", "checked_locally": True}, 400)
            return

        if voice_exists(voice_id):
            self._send_json(
                {"ok": False, "error": f"voice_id「{voice_id}」已存在于本地台账，换一个或先删除原记录"}, 400
            )
            return

        demo_text = (body.get("demo_text") or "").strip()
        if demo_text and len(demo_text) > 1000:
            self._send_json({"ok": False, "error": "试听文本超过 1000 字符上限"}, 400)
            return

        text_validation = (body.get("text_validation") or "").strip()
        if len(text_validation) > 200:
            self._send_json({"ok": False, "error": "text_validation 超过 200 字符上限"}, 400)
            return

        prompt_file_id = body.get("prompt_file_id")
        prompt_text = (body.get("prompt_text") or "").strip()
        if prompt_file_id and not prompt_text:
            self._send_json({"ok": False, "error": "上传了示例音频就必须填写其对应文本 prompt_text"}, 400)
            return

        payload = mc.build_clone_payload(
            file_id=int(file_id),
            voice_id=voice_id,
            model=body.get("model"),
            demo_text=demo_text or None,
            prompt_file_id=int(prompt_file_id) if prompt_file_id else None,
            prompt_text=prompt_text or None,
            language_boost=body.get("language_boost") or None,
            text_validation=text_validation or None,
            accuracy=body.get("accuracy"),
            need_noise_reduction=body.get("need_noise_reduction"),
            need_volume_normalization=body.get("need_volume_normalization"),
            aigc_watermark=body.get("aigc_watermark"),
        )

        result = mc.voice_clone(api_key=key, payload=payload, timeout=120)

        # 无论试听音频是否下载成功，voice_id 都要落台账，避免结果丢失
        record = upsert_voice(voice_id)

        demo_url = ""
        demo_warning = ""
        if result["demo_audio"]:
            try:
                audio = mc.download_file(result["demo_audio"], timeout=120)
                fname = save_audio(voice_id, "demo", audio, "mp3")
                demo_url = f"/audio/{fname}"
            except mc.MinimaxError as exc:
                demo_warning = f"试听音频未能下载到本地：{exc.message}（voice_id 已保存，可用合成页补生成）"
        elif demo_text:
            demo_warning = "服务端未返回试听音频"

        self._send_json(
            {
                "ok": True,
                "voice_id": result["voice_id"],
                "demo_audio_url": demo_url,
                "demo_warning": demo_warning,
                "extra_info": result["extra_info"],
                "input_sensitive_type": result["input_sensitive_type"],
                "input_sensitive_label": result["input_sensitive_label"],
                "record": record,
                "trace_id": result["trace_id"],
            }
        )

    def _handle_tts(self) -> None:
        body = self._read_json()
        key = mc.load_api_key()
        if not key:
            self._send_json({"ok": False, "error": "未配置 MINIMAX_API_KEY，请在 .env 中填写"}, 400)
            return

        voice_id = (body.get("voice_id") or "").strip()
        text = body.get("text") or ""
        if not voice_id:
            self._send_json({"ok": False, "error": "请选择要使用的音色 voice_id"}, 400)
            return
        if not text.strip():
            self._send_json({"ok": False, "error": "请输入要合成的文本"}, 400)
            return
        if len(text) >= 10000:
            self._send_json({"ok": False, "error": "文本接近 10000 字符上限，请分段合成"}, 400)
            return

        if voice_exists(voice_id):
            upsert_voice(voice_id, last_used=True)

        rules = [r.strip() for r in (body.get("pronunciation_rules") or []) if r.strip() and "/" in r.strip()]
        audio_format = body.get("audio_format") or "mp3"

        payload = mc.build_tts_payload(
            voice_id=voice_id,
            text=text,
            model=body.get("model") or "speech-2.8-hd",
            language_boost=body.get("language_boost") or None,
            speed=body.get("speed"),
            vol=body.get("vol"),
            pitch=body.get("pitch"),
            emotion=body.get("emotion") or None,
            audio_format=audio_format,
            sample_rate=int(body.get("sample_rate") or 32000),
            channel=int(body.get("channel") or 1),
            pronunciation_rules=rules or None,
            aigc_watermark=body.get("aigc_watermark"),
        )

        result = mc.text_to_speech(api_key=key, payload=payload, timeout=300)
        fname = save_audio(voice_id, "tts", result["audio"], audio_format)

        self._send_json(
            {
                "ok": True,
                "audio_url": f"/audio/{fname}",
                "filename": fname,
                "bytes": len(result["audio"]),
                "extra_info": result["extra_info"],
                "trace_id": result["trace_id"],
            }
        )


# ---------------------------------------------------------------- 启动


def find_port(start: int = 8765, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"[启动失败] {start}-{start + tries - 1} 端口均被占用")


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniMax 音色克隆本地网页工具")
    parser.add_argument("--port", type=int, default=None, help="指定端口，默认从 8765 起自动探测")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    ensure_dirs()
    clear_tmp()

    port = args.port or find_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True

    key = mc.load_api_key()
    url = f"http://127.0.0.1:{port}/"
    print("=" * 62)
    print("  MiniMax 音色克隆工具")
    print("=" * 62)
    print(f"  界面地址 : {url}")
    print(f"  接口地址 : {mc.BASE_URL}  (region=cn)")
    if key:
        print(f"  API Key  : {mc.key_hint(key)}  已配置")
    else:
        print("  API Key  : 未配置 —— 请在 .env 中写入 MINIMAX_API_KEY")
    print("  计费说明 : 本工具按量计费，直接扣除账户余额")
    print("  停止服务 : Ctrl + C")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
        clear_tmp()


if __name__ == "__main__":
    sys.exit(main())
