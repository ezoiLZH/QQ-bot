"""
视觉识别模块（API 版）—— 直接调用 DeepSeek Anthropic 兼容接口识别图片/表情包
与 vision.py 的本地 Qwen2-VL 实现保持相同接口，便于对比测试和后续切换。
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
from io import BytesIO

from PIL import Image

import config


# ==================== API 配置 ====================

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-flash[1M]"

# 与 vision.py 的本地模型使用同一套识别 prompt
_PROMPTS = {
    "sticker_content": ("描述这张表情包：简要说出说表情包的内容和情绪（10个字以内），不识别文字。只输出短语，不要解释。", 30),
    "sticker_text":    ("提取这张表情包上的所有文字。如果没有文字就说'无文字'。直接输出文字，不要解释。", 40),
    "image_classify":  ("对这张图片进行分类，只输出一个类别名称：风景、美食、二次元图片、游戏截图、视频截图、聊天截图、文字截图、梗图/meme、其他。不要解释，不要描述内容。", 15),
    "image_describe":  ("尽力描述这张图片的内容（包括文字）。直接输出描述，不要解释。", 250),
    "image_ocr":       ("像OCR一样提取这张图片中的所有文字，包括字幕、UI文字、标题、正文、聊天记录、评论等。如果没有文字就说'无文字'。直接输出文字，不要解释。", 200),
}


def _api_settings() -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)。优先 config 显式配置，其次继承环境变量。"""
    base_url = getattr(config, "VISION_API_BASE_URL", "") or os.getenv("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL)
    api_key = (
        getattr(config, "VISION_API_KEY", "")
        or getattr(config, "CLAUDE_API_KEY", "")
        or os.getenv("ANTHROPIC_AUTH_TOKEN", "")
        or os.getenv("ANTHROPIC_API_KEY", "")
    )
    model = getattr(config, "VISION_API_MODEL", "") or getattr(config, "CLAUDE_MODEL", "") or DEFAULT_MODEL
    return base_url.rstrip("/"), api_key, model


# ==================== 图片准备 ====================

def _to_rgb(image: Image.Image) -> Image.Image:
    """统一转 RGB（调色板图先转 RGBA，避免透明通道警告）。"""
    if image.mode == "P":
        image = image.convert("RGBA")
    return image.convert("RGB")


def _resize(image: Image.Image, *, is_sticker: bool = False) -> Image.Image:
    """按配置缩放单帧（与 vision.py 的缩放策略一致）。"""
    w, h = image.size
    max_area = config.STICKER_MAX_AREA if is_sticker else config.VISION_MAX_AREA
    if w * h > max_area:
        scale = math.sqrt(max_area / (w * h))
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    return image


def _prepare_frames(img_bytes: bytes, *, is_sticker: bool = False) -> list[Image.Image]:
    """解码图片；动图（GIF/WebP）按上限均匀抽帧，静态图返回 1 帧。"""
    src = Image.open(BytesIO(img_bytes))
    n_frames = getattr(src, "n_frames", 1)
    max_frames = max(1, int(getattr(config, "VISION_MAX_FRAMES", 3)))

    if n_frames <= 1:
        indices = [0]
    else:
        k = min(n_frames, max_frames)
        if k == 1:
            indices = [0]
        else:
            # 固定首帧和尾帧，中间均匀取点（四舍五入，避免 Python round 的银行家舍入）
            indices = sorted({
                (2 * i * (n_frames - 1) + (k - 1)) // (2 * (k - 1))
                for i in range(k)
            })

    frames: list[Image.Image] = []
    for idx in indices:
        src.seek(idx)
        frames.append(_resize(_to_rgb(src), is_sticker=is_sticker))
    return frames


# ==================== API 推理 ====================

async def _api_infer(frames: list[Image.Image], mode: str) -> str | None:
    """把图片（单帧或多帧）编码成 base64 发给 DeepSeek，返回模型文本输出。"""
    base_url, api_key, model = _api_settings()
    if not api_key:
        print("🖼️ [API] 未找到 API key（config.claude.api_key 或 ANTHROPIC_AUTH_TOKEN）")
        return None

    prompt, max_tokens = _PROMPTS[mode]
    if len(frames) > 1:
        prompt = (
            f"以下 {len(frames)} 张图是同一段动图按时间顺序均匀抽取的帧"
            f"（第 1 帧到第 {len(frames)} 帧），请综合所有帧判断动画内容。\n{prompt}"
        )

    content: list[dict] = []
    for image in frames:
        buf = BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=85)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(buf.getvalue()).decode(),
            },
        })
    content.append({"type": "text", "text": prompt})

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": config.VISION_TEMPERATURE,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": content}],
    }

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/v1/messages",
                json=payload,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    print(f"🖼️ [API] 识别失败[{mode}] HTTP {resp.status}: {body[:200]}")
                    return None
                data = json.loads(body)
    except Exception as e:
        print(f"🖼️ [API] 识别失败[{mode}]: {e}")
        return None

    usage = data.get("usage", {})
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    print(
        f"🖼️ [API] {mode} frames={len(frames)} tokens: in={usage.get('input_tokens')} "
        f"out={usage.get('output_tokens')} stop={data.get('stop_reason')}"
    )

    if not text:
        print(f"🖼️ [API] 图片识别[{mode}]: 无文本（stop={data.get('stop_reason')}）")
        return None

    desc = text.replace("\n", " ")
    desc = desc.strip("\"'「」『』[]（）()，。、* \t\n\r")
    if not desc or desc in ("无文字", "无", "无文字。"):
        return None
    print(f"🖼️ [API] 图片识别[{mode}]: {desc}")
    return desc


# ==================== 对外接口 ====================

async def describe_image_bytes(img_bytes: bytes, *, is_sticker: bool = False) -> str | None:
    """直接对图片字节做识别（测试/内部调用用）。动图会自动抽帧。"""
    frames = _prepare_frames(img_bytes, is_sticker=is_sticker)

    if is_sticker:
        content = await _api_infer(frames, "sticker_content")
        ocr = await _api_infer(frames, "sticker_text")
        if content and ocr:
            return f'{content} "{ocr}"'
        return content or (f'"{ocr}"' if ocr else None)

    # 分类只看首帧，描述看全部抽帧
    classify = await _api_infer(frames[:1], "image_classify")
    describe = await _api_infer(frames, "image_describe")
    parts = []
    if classify:
        parts.append(classify)
    if describe:
        parts.append(f" {describe}")
    return " ".join(parts) if parts else None


async def _describe_image_url(url: str, *, is_sticker: bool = False) -> str | None:
    """下载图片并识别。接口与 vision._describe_image_url 保持一致，可直接替换调用。"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                img_bytes = await resp.read()
                if len(img_bytes) > 5 * 1024 * 1024:
                    return None
    except Exception:
        return None

    return await describe_image_bytes(img_bytes, is_sticker=is_sticker)
