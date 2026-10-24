"""
视觉识别模块 —— Qwen2-VL 本地模型，图片/表情包描述
"""
from __future__ import annotations

import asyncio
import re
import math
from io import BytesIO

from PIL import Image

import config


# ==================== 模型管理 ====================

_vision_pipe = None
_vision_lock = None


def _get_vision_lock():
    import threading
    global _vision_lock
    if _vision_lock is None:
        _vision_lock = threading.Lock()
    return _vision_lock


def _get_vision_pipe():
    global _vision_pipe
    if _vision_pipe is not None:
        return _vision_pipe

    if not config.VISION_ENABLED:
        print("🖼️ 视觉模型已禁用")
        return None

    import torch
    from transformers import pipeline

    cuda_available = torch.cuda.is_available()
    if cuda_available and not config.VISION_FORCE_CPU:
        device = 0
        dtype = torch.float16
        device_name = torch.cuda.get_device_name(0)
        print(f"🖼️ CUDA 可用: {device_name}")
    else:
        if config.VISION_FORCE_CPU:
            print("🖼️ 强制 CPU 模式")
        elif not cuda_available:
            print("🖼️ CUDA 不可用，使用 CPU 推理")
        device = -1
        dtype = torch.float32

    print(f"🖼️ 加载视觉模型 {config.VISION_MODEL_ID} (device={'cuda' if device >= 0 else 'cpu'})...")
    _vision_pipe = pipeline(
        "image-text-to-text",
        model=config.VISION_MODEL_ID,
        dtype=dtype,
        device=device,
    )
    print("🖼️ 视觉模型就绪")
    return _vision_pipe


def _unload_vision_pipe():
    global _vision_pipe
    if _vision_pipe is not None:
        print("🖼️ 卸载视觉模型...")
        del _vision_pipe
        _vision_pipe = None
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        print("🖼️ 视觉模型已卸载")


# ==================== 图片描述 ====================

async def _describe_image_url(url: str, *, is_sticker: bool = False) -> str | None:
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

    image = Image.open(BytesIO(img_bytes)).convert("RGB")
    w, h = image.size
    max_area = config.STICKER_MAX_AREA if is_sticker else config.VISION_MAX_AREA
    if w * h > max_area:
        scale = math.sqrt(max_area / (w * h))
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    loop = asyncio.get_running_loop()

    if is_sticker:
        content = await loop.run_in_executor(None, _vision_infer, image, "sticker_content")
        ocr = await loop.run_in_executor(None, _vision_infer, image, "sticker_text")
        if content and ocr:
            return f'{content} "{ocr}"'
        return content or f'"{ocr}"'
    else:
        # 普通图片：2 轮识别 —— 分类 → 描述内容
        classify = await loop.run_in_executor(None, _vision_infer, image, "image_classify")
        describe = await loop.run_in_executor(None, _vision_infer, image, "image_describe")
        # ocr = await loop.run_in_executor(None, _vision_infer, image, "image_ocr")
        parts = []
        if classify:
            parts.append(f"{classify}")
        if describe:
            parts.append(f" {describe}")
        # if ocr:
        #     parts.append(f' "{ocr}"')
        return " ".join(parts) if parts else None


def _vision_infer(image, mode: str) -> str | None:
    if not config.VISION_ENABLED:
        return None
    _PROMPTS = {
        "sticker_content": ("描述这张表情包：简要说出说表情包的内容和情绪（10个字以内），不识别文字。只输出短语，不要解释。", 30),
        "sticker_text":    ("提取这张表情包上的所有文字。如果没有文字就说'无文字'。直接输出文字，不要解释。", 40),
        "image_classify":  ("对这张图片进行分类，只输出一个类别名称：风景、美食、二次元图片、游戏截图、视频截图、聊天截图、文字截图、梗图/meme、其他。不要解释，不要描述内容。", 15),
        "image_describe":  ("尽力描述这张图片的内容（包括文字）。直接输出描述，不要解释。", 250),
        "image_ocr":       ("像OCR一样提取这张图片中的所有文字，包括字幕、UI文字、标题、正文、聊天记录、评论等。如果没有文字就说'无文字'。直接输出文字，不要解释。", 200),
    }
    prompt, max_tokens = _PROMPTS[mode]

    try:
        pipe = _get_vision_pipe()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        with _get_vision_lock():
            result = pipe(messages, max_new_tokens=max_tokens, do_sample=True, temperature=config.VISION_TEMPERATURE)
        gen = result[0]["generated_text"] if isinstance(result, list) else []
        if isinstance(gen, list):
            for msg in reversed(gen):
                if msg.get("role") == "assistant":
                    desc = msg.get("content", "")
                    break
            else:
                desc = ""
        else:
            desc = str(gen)
        desc = desc.strip().replace("\n", " ")
        desc = desc.strip("\"'「」『』[]（）()，。、* \t\n\r")
        if not desc or desc in ("无文字", "无", "无文字。"):
            return None
        if desc:
            print(f"🖼️ 图片识别[{mode}]: {desc}")
            return desc
        else: 
            print(f"🖼️ 图片识别[{mode}]: 无")
    except Exception as e:
        print(f"🖼️ 图片识别失败[{mode}]: {e}")

    return None


