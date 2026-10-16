"""MiniMax 开放平台语音接口封装：文件上传、音色克隆、语音合成。

约束：
- API Key 只在本模块读取，永不写入日志、永不随错误信息返回。
- 任何失败都直接抛出，由调用方转为提示；**不做自动重试**（本路径按量计费，重试等于重复扣费）。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

BASE_URL = "https://api.minimax.cn"

# 官方错误码 -> 面向用户的中文说明
ERROR_MESSAGES: dict[int, str] = {
    1000: "未知错误，请稍后手动重试",
    1001: "请求超时。已自动停止，不会自动重发以免重复计费，请确认网络后手动重试",
    1002: "触发限流，请等待约 1 分钟再手动重试（不会自动重试，以免重复计费）",
    1004: "API Key 无效，请检查 .env 中的 MINIMAX_API_KEY",
    1008: "账户余额不足，请在开放平台充值后重试",
    1013: "服务端内部错误，请稍后手动重试或联系官方",
    1026: "输入内容错误",
    1027: "输出内容错误",
    1039: "触发 TPM 限流，请降低请求频率",
    1042: "非法字符超过 10%，请检查合成文本",
    1043: "素材文本与 text_validation 不符，请核对素材内容或调整 accuracy 阈值",
    2013: "输入格式或参数不正常，通常是音频格式、时长或大小不符，请对照 README 规格",
    2038: "无复刻权限。音色克隆要求账号已完成实名认证，请前往开放平台「账户管理 > 账户信息」完成认证",
}

# 风控类型（input_sensitive_type）
SENSITIVE_TYPES = {
    1: "严重违规",
    2: "色情",
    3: "广告",
    4: "违禁",
    5: "谩骂",
    6: "暴恐",
    7: "其他",
}

T2A_MODELS = (
    "speech-2.8-hd",
    "speech-2.8-turbo",
    "speech-2.6-hd",
    "speech-2.6-turbo",
    "speech-02-hd",
    "speech-02-turbo",
    "speech-01-hd",
    "speech-01-turbo",
)

LANGUAGE_BOOST_OPTIONS = (
    "auto",
    "Chinese",
    "Chinese,Yue",
    "Japanese",
    "Korean",
    "English",
    "Arabic",
    "Russian",
    "Spanish",
    "French",
    "Portuguese",
    "German",
    "Turkish",
    "Dutch",
    "Ukrainian",
    "Vietnamese",
    "Indonesian",
    "Italian",
    "Thai",
    "Polish",
    "Romanian",
    "Greek",
    "Czech",
    "Finnish",
    "Hindi",
    "Bulgarian",
    "Danish",
    "Hebrew",
    "Malay",
    "Persian",
    "Slovak",
    "Swedish",
    "Croatian",
    "Filipino",
    "Hungarian",
    "Norwegian",
    "Slovenian",
    "Catalan",
    "Nynorsk",
    "Tamil",
    "Afrikaans",
)

AUDIO_FORMATS = ("mp3", "wav", "flac", "pcm", "opus")

EMOTIONS = ("happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm", "fluent", "whisper")


class MinimaxError(Exception):
    """接口调用失败。code 为业务状态码，0 之外的整数。"""

    def __init__(self, message: str, code: int = -1, trace_id: str = "", raw: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.trace_id = trace_id
        self.raw = raw

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.message,
            "status_code": self.code,
            "trace_id": self.trace_id,
            "raw": self.raw,
        }


def load_api_key(env_path: str | None = None) -> str:
    """从 .env 读取 MINIMAX_API_KEY。文件不存在或为空时返回空串。"""
    path = env_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "MINIMAX_API_KEY":
                    return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def key_hint(api_key: str) -> str:
    """只暴露末尾 4 位，用于界面提示。绝不出现在任何错误信息里。"""
    if not api_key:
        return ""
    tail = api_key[-4:]
    return f"****{tail}" if len(api_key) > 4 else "****"


def _open(req, timeout):  # 单独抽出便于测试打桩
    return urlrequest.urlopen(req, timeout=timeout)


def _extract_trace_id(headers) -> str:
    if headers is None:
        return ""
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for name, value in items:
        if name.lower() in ("x-trace-id", "trace-id", "traceid"):
            return value
    return ""


def _request_json(
    path: str,
    *,
    api_key: str,
    payload: dict[str, Any] | None = None,
    body: bytes | None = None,
    content_type: str = "application/json",
    timeout: int = 60,
) -> dict[str, Any]:
    """统一请求入口。失败一律抛 MinimaxError，绝不重试。"""
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
    }
    data = body if body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
    req = urlrequest.Request(url, data=data, headers=headers, method="POST")

    try:
        resp = _open(req, timeout)
    except HTTPError as exc:
        trace_id = _extract_trace_id(getattr(exc, "headers", None))
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001 - 仅用于尽力提取错误正文
            pass
        raise MinimaxError(
            f"HTTP {exc.code}：请求未能送达服务端（{trace_id or '无 trace_id'}）",
            code=exc.code,
            trace_id=trace_id,
            raw=detail,
        ) from None
    except URLError:
        raise MinimaxError("网络连接失败，请检查网络或代理设置后手动重试", code=-2) from None
    except TimeoutError:
        raise MinimaxError("请求超时，已停止且不会自动重试，请手动重试", code=-3) from None

    with resp:
        raw_body = resp.read().decode("utf-8", "replace")
        trace_id = _extract_trace_id(getattr(resp, "headers", None))

    try:
        result = json.loads(raw_body)
    except json.JSONDecodeError:
        raise MinimaxError(
            f"服务端返回了非 JSON 内容（{trace_id or '无 trace_id'}）",
            code=-4,
            trace_id=trace_id,
            raw=raw_body[:500],
        ) from None

    base = result.get("base_resp") or {}
    code = int(base.get("status_code", -1))
    if code != 0:
        message = ERROR_MESSAGES.get(code, f"接口返回状态码 {code}")
        raw_msg = base.get("status_msg") or ""
        raise MinimaxError(
            message,
            code=code,
            trace_id=trace_id,
            raw=raw_msg,
        )

    result["_trace_id"] = trace_id
    return result


def _build_multipart(fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    """urllib 无原生 multipart 支持，手工拼装。注意全程二进制。"""
    boundary = "----minimaxvoice" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_file(
    *,
    api_key: str,
    file_bytes: bytes,
    filename: str,
    purpose: str,
    timeout: int = 60,
) -> dict[str, Any]:
    """上传待复刻音频或示例音频，返回 file 信息。"""
    body, content_type = _build_multipart({"purpose": purpose}, "file", filename, file_bytes)
    result = _request_json(
        "/v1/files/upload",
        api_key=api_key,
        body=body,
        content_type=content_type,
        timeout=timeout,
    )
    file_obj = result.get("file") or {}
    if not file_obj.get("file_id"):
        raise MinimaxError("上传成功但未返回 file_id", code=-5, trace_id=result.get("_trace_id", ""))
    return {
        "file_id": file_obj["file_id"],
        "bytes": file_obj.get("bytes", len(file_bytes)),
        "filename": file_obj.get("filename", filename),
        "trace_id": result.get("_trace_id", ""),
    }


def build_clone_payload(
    *,
    file_id: int,
    voice_id: str,
    model: str | None = None,
    demo_text: str | None = None,
    prompt_file_id: int | None = None,
    prompt_text: str | None = None,
    language_boost: str | None = None,
    text_validation: str | None = None,
    accuracy: float | None = None,
    need_noise_reduction: bool | None = None,
    need_volume_normalization: bool | None = None,
    aigc_watermark: bool | None = None,
) -> dict[str, Any]:
    """构造克隆请求体。

    关键规则：不传 demo_text 时，请求体中既不含 text 也不含 model，
    服务端因此不触发试听合成、不产生合成费用。
    """
    payload: dict[str, Any] = {"file_id": file_id, "voice_id": voice_id}

    if prompt_file_id is not None:
        clone_prompt: dict[str, Any] = {"prompt_audio": prompt_file_id}
        if prompt_text:
            clone_prompt["prompt_text"] = prompt_text
        payload["clone_prompt"] = clone_prompt

    if demo_text:
        payload["text"] = demo_text
        # 官方要求：提供 text 时 model 必传
        payload["model"] = model or "speech-2.8-hd"

    if language_boost:
        payload["language_boost"] = language_boost
    if text_validation:
        payload["text_validation"] = text_validation
    if accuracy is not None:
        payload["accuracy"] = accuracy
    if need_noise_reduction is not None:
        payload["need_noise_reduction"] = bool(need_noise_reduction)
    if need_volume_normalization is not None:
        payload["need_volume_normalization"] = bool(need_volume_normalization)
    if aigc_watermark is not None:
        payload["aigc_watermark"] = bool(aigc_watermark)

    return payload


def voice_clone(*, api_key: str, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    result = _request_json("/v1/voice_clone", api_key=api_key, payload=payload, timeout=timeout)
    sensitive_type = result.get("input_sensitive_type")
    return {
        "voice_id": payload["voice_id"],
        "demo_audio": result.get("demo_audio") or "",
        "extra_info": result.get("extra_info") or {},
        "input_sensitive_type": sensitive_type,
        "input_sensitive_label": SENSITIVE_TYPES.get(sensitive_type, "") if sensitive_type else "",
        "trace_id": result.get("_trace_id", ""),
    }


def build_tts_payload(
    *,
    voice_id: str,
    text: str,
    model: str = "speech-2.8-hd",
    language_boost: str | None = None,
    speed: float | None = None,
    vol: float | None = None,
    pitch: int | None = None,
    emotion: str | None = None,
    audio_format: str = "mp3",
    sample_rate: int = 32000,
    channel: int = 1,
    pronunciation_rules: list[str] | None = None,
    aigc_watermark: bool | None = None,
) -> dict[str, Any]:
    """构造语音合成请求体。固定非流式 + hex 输出，便于本地落盘。"""
    voice_setting: dict[str, Any] = {"voice_id": voice_id}
    if speed is not None:
        voice_setting["speed"] = speed
    if vol is not None:
        voice_setting["vol"] = vol
    if pitch is not None:
        voice_setting["pitch"] = pitch
    if emotion:
        voice_setting["emotion"] = emotion

    payload: dict[str, Any] = {
        "model": model,
        "text": text,
        "stream": False,
        "output_format": "hex",
        "voice_setting": voice_setting,
        "audio_setting": {
            "sample_rate": sample_rate,
            "format": audio_format,
            "channel": channel,
        },
    }
    if pronunciation_rules:
        payload["pronunciation_dict"] = {"tone": list(pronunciation_rules)}
    if language_boost:
        payload["language_boost"] = language_boost
    if aigc_watermark is not None:
        payload["aigc_watermark"] = bool(aigc_watermark)
    return payload


def decode_hex_audio(hex_text: str) -> bytes:
    """把 hex 编码的音频解码为字节。"""
    if not hex_text:
        raise MinimaxError("服务端未返回音频数据", code=-6)
    try:
        return bytes.fromhex(hex_text)
    except ValueError:
        raise MinimaxError("服务端返回的音频编码无法解析", code=-7) from None


def text_to_speech(*, api_key: str, payload: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    result = _request_json("/v1/t2a_v2", api_key=api_key, payload=payload, timeout=timeout)
    data = result.get("data") or {}
    audio = decode_hex_audio(data.get("audio") or "")
    extra = result.get("extra_info") or {}
    expected = extra.get("audio_size")
    if isinstance(expected, int) and expected != len(audio):
        raise MinimaxError(
            f"音频字节数与 extra_info 不一致（本地 {len(audio)} / 声明 {expected}）",
            code=-8,
            trace_id=result.get("_trace_id", ""),
        )
    return {
        "audio": audio,
        "extra_info": extra,
        "trace_id": result.get("_trace_id", ""),
    }


def download_file(url: str, timeout: int = 120) -> bytes:
    """下载试听音频。失败抛出 MinimaxError，由调用方降级处理。"""
    try:
        resp = _open(urlrequest.Request(url, method="GET"), timeout)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise MinimaxError("试听音频下载失败", code=-9) from None
    with resp:
        return resp.read()
