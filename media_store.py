"""
图片落盘与自适应分辨率 —— 原生多模态消息的图片准备。

收图时只做一次解码/缩放/编码，结果按内容哈希存到 data/images/<chat_id>/。
之后每次构建 prompt 直接复用同一份字节，保证前缀缓存命中。
"""
from __future__ import annotations

import base64
import hashlib
import math
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter

import config


IMAGES_DIR_NAME = "images"
LOG_TIME_FORMAT = "%m-%d %H:%M:%S"
MERGE_WINDOW_SECONDS = 30


def parse_log_time(value: str):
    """解析日志里的 'MM-DD HH:MM:SS'，失败返回 None。"""
    try:
        return datetime.strptime(value, LOG_TIME_FORMAT)
    except Exception:
        return None


CONTINUATION_PREFIX = "  "  # 同人 30 秒内续行：只缩进，不再显示时间

# 信息量判定阈值（基于缩略图的廉价指标，无需额外模型调用）
_TEXT_SHORT_RUN = 0.78   # 短色块占比高 → 文字密集
_TEXT_MAX_UNIQ = 0.05    # 文字截图颜色种类很少
_SIMPLE_SHORT_RUN = 0.70
_SIMPLE_MAX_UNIQ = 0.20


def safe_chat_id(chat_id: str | None) -> str:
    """与聊天日志文件名一致的会话目录名。"""
    if not chat_id:
        return "_misc"
    return re.sub(r"[^A-Za-z0-9_-]", "_", chat_id)


def images_dir(chat_id: str | None = None) -> Path:
    d = config.DATA_DIR / IMAGES_DIR_NAME / safe_chat_id(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_blocks(text: str, images: list[dict] | None = None, *, prefix: str = "") -> list[dict]:
    """把消息正文里的图片占位符替换成 image blocks。

    text 只包含消息正文；prefix 是 "[时间] 昵称: " 前缀，不参与占位符匹配。
    支持 `[图片#0]`（按 id 精确配对）和旧日志的 `[图片]`（按顺序配对）。
    图片文件缺失时保留原占位符文字。
    """
    images = images or []
    image_map = {str(m.get("id", i)): m for i, m in enumerate(images)}
    has_indexed = bool(re.search(r"\[(?:图片|动画表情)#\d+\]", text))
    img_pos = 0
    blocks: list[dict] = []
    text_buf = prefix
    last_end = 0

    for match in re.finditer(r"\[(图片|动画表情)(?:#(\d+))?\]", text):
        text_buf += text[last_end:match.start()]
        last_end = match.end()

        id_str = match.group(2)
        meta = None
        if id_str is not None:
            meta = image_map.get(id_str)  # 按编号精确定位
        elif not has_indexed and img_pos < len(images):
            meta = images[img_pos]  # 兼容旧日志：按顺序配对
            img_pos += 1

        if meta and meta.get("files"):
            frames: list[bytes] = []
            for rel in meta["files"]:
                try:
                    frames.append((config.DATA_DIR / rel).read_bytes())
                except OSError:
                    continue

            if frames:
                # 用 [类型：图片块...] 的形式把图片/表情包包起来，让模型明确类型和边界
                if len(frames) > 1:
                    kind = "动画表情" if meta.get("is_sticker") else "动图"
                    opener = f"[{kind} {len(frames)} 帧："
                else:
                    opener = "[动画表情：" if meta.get("is_sticker") else "[图片："
                text_buf += opener
                if text_buf:
                    blocks.append({"type": "text", "text": text_buf})
                    text_buf = ""
                for raw in frames:
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(raw).decode(),
                        },
                    })
                text_buf = "]"  # 闭合标记，与后续文字合并
            else:
                text_buf += match.group(0)  # 文件全丢失时保留占位符
        else:
            text_buf += match.group(0)  # 图片缺失时保留占位符文字

    text_buf += text[last_end:]
    if text_buf:
        blocks.append({"type": "text", "text": text_buf})
    return blocks


def _to_rgb(image: Image.Image) -> Image.Image:
    """统一转 RGB；带透明通道的图合成到白底上。"""
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        bg = Image.new("RGB", image.size, (255, 255, 255))
        bg.paste(image, mask=image.split()[-1])
        return bg
    return image.convert("RGB")


def _edge_density(image: Image.Image) -> float:
    g = image.convert("L")
    g.thumbnail((256, 256))
    hist = g.filter(ImageFilter.FIND_EDGES).histogram()
    total = g.width * g.height
    return sum(hist[32:]) / total if total else 0.0


def _unique_ratio(image: Image.Image) -> float:
    s = image.convert("RGB")
    s.thumbnail((128, 128))
    colors = s.getcolors(maxcolors=128 * 128)
    n = len(colors) if colors is not None else 128 * 128
    return n / (s.width * s.height) if s.width * s.height else 1.0


def _short_run_ratio(image: Image.Image) -> float:
    """水平方向短色块占比：文字笔画多短跑，扁平色块多长跑。"""
    im = image.convert("RGB")
    im.thumbnail((256, 256))
    w, h = im.size
    data = list(im.getdata())
    short = total = 0
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        prev = None
        run = 0
        runs = []
        for r, g, b in row:
            key = (r // 32, g // 32, b // 32)
            if key == prev:
                run += 1
            else:
                if prev is not None:
                    runs.append(run)
                prev = key
                run = 1
        runs.append(run)
        short += sum(1 for ln in runs if ln <= 4)
        total += len(runs)
    return short / total if total else 0.0


def classify_tier(image: Image.Image) -> str:
    """返回 simple / normal / dense。"""
    if not config.VISION_ADAPTIVE_RESOLUTION:
        return "dense"
    edge = _edge_density(image)
    uniq = _unique_ratio(image)
    short = _short_run_ratio(image)
    if uniq < _TEXT_MAX_UNIQ and short > _TEXT_SHORT_RUN:
        return "dense"
    if uniq < _SIMPLE_MAX_UNIQ and short < _SIMPLE_SHORT_RUN:
        return "simple"
    # 照片等细节图走 normal；边缘特别密的也归 dense
    if edge > 0.20:
        return "dense"
    return "normal"


def _max_edge(tier: str, is_sticker: bool) -> int:
    if is_sticker:
        edge = int(math.sqrt(config.STICKER_MAX_AREA))
    else:
        edge = {
            "simple": config.VISION_RES_SIMPLE,
            "normal": config.VISION_RES_NORMAL,
            "dense": config.VISION_RES_DENSE,
        }.get(tier, config.VISION_RES_DENSE)
    area_limit = config.STICKER_MAX_AREA if is_sticker else config.VISION_MAX_AREA
    return max(1, min(edge, int(math.sqrt(area_limit))))


def _resize_to_edge(image: Image.Image, max_edge: int) -> Image.Image:
    w, h = image.size
    longest = max(w, h)
    if longest <= max_edge:
        return image  # 不放大
    scale = max_edge / longest
    return image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def _frame_indices(n_frames: int) -> list[int]:
    max_frames = max(1, int(getattr(config, "VISION_MAX_FRAMES", 3)))
    if n_frames <= 1:
        return [0]
    k = min(n_frames, max_frames)
    if k == 1:
        return [0]
    return sorted({(2 * i * (n_frames - 1) + (k - 1)) // (2 * (k - 1)) for i in range(k)})


def encode_frames(img_bytes: bytes, *, is_sticker: bool = False,
                  chat_id: str | None = None) -> dict | None:
    """解码图片（动图抽帧）、自适应缩放、编码 JPEG 并按内容哈希落盘。

    图片保存在 data/images/<chat_id>/ 下，返回的 files 是相对 DATA_DIR 的路径：
    {"files": ["images/<chat_id>/<sha1>.jpg", ...], "tier": str, "max_edge": int}。
    失败返回 None。
    """
    try:
        src = Image.open(BytesIO(img_bytes))
        indices = _frame_indices(getattr(src, "n_frames", 1))

        src.seek(indices[0])
        first = _to_rgb(src)
        tier = classify_tier(first)
        max_edge = _max_edge(tier, is_sticker)

        out_dir = images_dir(chat_id)
        rel_dir = f"{IMAGES_DIR_NAME}/{safe_chat_id(chat_id)}"
        files: list[str] = []
        for idx in indices:
            src.seek(idx)
            img = _resize_to_edge(_to_rgb(src), max_edge)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=85)
            raw = buf.getvalue()
            name = hashlib.sha1(raw).hexdigest() + ".jpg"
            path = out_dir / name
            if not path.exists():
                path.write_bytes(raw)
            files.append(f"{rel_dir}/{name}")

        print(f"🖼️ 图片落盘: {safe_chat_id(chat_id)} {len(files)}帧 tier={tier} max_edge={max_edge}")
        return {"files": files, "tier": tier, "max_edge": max_edge, "is_sticker": is_sticker}
    except Exception as e:
        print(f"🖼️ 图片落盘失败: {e}")
        return None
