"""本地前置校验：voice_id 命名规则、音频规格、时长解析。

本模块只做本地判断，不发起任何网络请求。
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess

# 官方仅接受 mp3 / m4a / wav
ALLOWED_EXTS = (".mp3", ".m4a", ".wav")

# 官方限制：待复刻音频与示例音频均不超过 20 MB
MAX_BYTES = 20 * 1024 * 1024

# 官方限制：待复刻音频 10 秒 ~ 5 分钟
CLONE_MIN_SECONDS = 10.0
CLONE_MAX_SECONDS = 300.0

# 官方限制：示例音频小于 8 秒
PROMPT_MAX_SECONDS = 8.0

# voice_id 命名规则
VOICE_ID_MIN_LEN = 8
VOICE_ID_MAX_LEN = 256

_PURPOSE_RULES = {
    "voice_clone": (CLONE_MIN_SECONDS, CLONE_MAX_SECONDS),
    "prompt_audio": (0.0, PROMPT_MAX_SECONDS),
}


def validate_voice_id(voice_id: str) -> tuple[bool, str]:
    """校验自定义 voice_id。

    官方规则：长度 8-256；首字符必须为英文字母；仅允许数字、字母、-、_；
    末位不可为 - 或 _。
    """
    if not isinstance(voice_id, str) or not voice_id:
        return False, "voice_id 不能为空"

    if not (VOICE_ID_MIN_LEN <= len(voice_id) <= VOICE_ID_MAX_LEN):
        return False, f"长度需在 {VOICE_ID_MIN_LEN}-{VOICE_ID_MAX_LEN} 之间，当前 {len(voice_id)}"

    if not voice_id[0].isascii() or not voice_id[0].isalpha():
        return False, "首字符必须是英文字母"

    illegal = [c for c in voice_id if not (c.isascii() and (c.isalnum() or c in "-_"))]
    if illegal:
        return False, f"包含非法字符：{''.join(sorted(set(illegal)))}（仅允许字母、数字、-、_）"

    if voice_id[-1] in "-_":
        return False, "末位字符不可为 - 或 _"

    return True, "OK"


def _rule_for(purpose: str) -> tuple[float, float] | None:
    return _PURPOSE_RULES.get(purpose)


def describe_rules(purpose: str) -> str:
    """给人看的规格描述，用于页面提示。"""
    if purpose == "voice_clone":
        return "mp3 / m4a / wav，时长 10 秒 ~ 5 分钟，大小不超过 20 MB"
    if purpose == "prompt_audio":
        return "mp3 / m4a / wav，时长小于 8 秒，大小不超过 20 MB"
    return "未知用途"


def parse_wav_duration(data: bytes) -> float | None:
    """从 WAV 字节流解析时长（秒）。非 WAV 或解析失败返回 None。

    按 fmt 块的 byte_rate 计算 data 块可容纳的采样时长。
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None

    pos = 12
    byte_rate = None
    data_size = None

    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        try:
            chunk_size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        except struct.error:
            break

        body = pos + 8
        if chunk_id == b"fmt " and body + 16 <= len(data):
            # 字段顺序：audio_format, channels, sample_rate, byte_rate, block_align, bits
            _, _, _, byte_rate, _, _ = struct.unpack("<HHIIHH", data[body : body + 16])
        elif chunk_id == b"data":
            data_size = min(chunk_size, len(data) - body)
            break

        # chunk 按偶数字节对齐
        pos = body + chunk_size + (chunk_size % 2)

    if not byte_rate or data_size is None:
        return None
    return data_size / float(byte_rate)


def probe_duration(path: str) -> float | None:
    """用 ffprobe 测量任意格式时长。ffprobe 不可用或失败时返回 None。"""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def measure_duration(path: str, head: bytes) -> float | None:
    """优先用 WAV 头本地解析，其次用 ffprobe。都失败返回 None。"""
    if path.lower().endswith(".wav"):
        parsed = parse_wav_duration(head)
        if parsed is not None:
            return parsed
    return probe_duration(path)


def validate_audio(
    filename: str,
    size: int,
    purpose: str,
    duration: float | None,
) -> tuple[bool, str]:
    """本地校验音频是否满足官方规格。返回 (是否通过, 说明)。

    duration 为 None 表示无法测量，此时跳过时长检查，由服务端最终校验。
    """
    rules = _rule_for(purpose)
    if rules is None:
        return False, f"未知的 purpose：{purpose}"

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTS:
        return False, f"不支持的格式「{ext or '无扩展名'}」，仅支持 {' / '.join(ALLOWED_EXTS)}"

    if size <= 0:
        return False, "文件为空"
    if size > MAX_BYTES:
        mb = size / (1024 * 1024)
        return False, f"文件大小 {mb:.1f} MB 超过 20 MB 上限"

    low, high = rules
    if duration is None:
        return True, "格式与大小校验通过；时长未能本地测量，将由服务端校验"

    if purpose == "prompt_audio":
        if duration >= PROMPT_MAX_SECONDS:
            return False, f"示例音频时长 {duration:.1f} 秒，需小于 8 秒"
        return True, f"格式、大小与时长校验通过（{duration:.1f} 秒）"

    if duration < low:
        return False, f"时长 {duration:.1f} 秒低于下限 10 秒"
    if duration > high:
        return False, f"时长 {duration:.1f} 秒超过上限 5 分钟"
    return True, f"格式、大小与时长校验通过（{duration:.1f} 秒）"


def count_charged_characters(text: str) -> int:
    """估算计费字符数。

    官方口径：1 个汉字算 2 个字符；英文字母、希腊字母、标点符号、特殊符号、
    空格、回车等各算 1 个字符。因此中文文本必须按 2 倍估算，否则会少算一半。

    这是保守估算，真实计费以服务端返回的 usage_characters 为准。
    """
    total = 0
    for ch in text:
        cp = ord(ch)
        # 表意文字与日韩文字按 2 字符计；标点（含全角标点）仍按 1 字符计
        if (
            0x3400 <= cp <= 0x4DBF      # 汉字扩展 A
            or 0x4E00 <= cp <= 0x9FFF   # 汉字基本区
            or 0xF900 <= cp <= 0xFAFF   # 兼容表意文字
            or 0x3040 <= cp <= 0x309F   # 平假名
            or 0x30A0 <= cp <= 0x30FF   # 片假名
        ):
            total += 2
        else:
            total += 1
    return total
