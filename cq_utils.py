"""
CQ 码工具模块 —— 消息解析、文本提取、表情贴纸、消息发送
"""
from __future__ import annotations

import asyncio
import json
import re
import random
from pathlib import Path
from urllib.parse import unquote

import aiohttp

import config
import storage
import vision
import media_store


# ==================== 表情贴纸 ====================

STICKERS_DIR = Path(__file__).parent / "stickers"


def _resolve_stickers(text: str, bot_name: str = "") -> str:
    """将 [动画表情:名字] / [sticker:名字] 替换为 [CQ:image,file=路径,subType=1]

    bot_name: 指定 bot 名称，会从 stickers/<bot_name>/ 下查找表情包。
              为空时回退到 stickers/ 根目录（兼容旧布局）。
    """
    sticker_root = STICKERS_DIR / bot_name if bot_name else STICKERS_DIR
    if not sticker_root.exists():
        return text

    name_to_files: dict[str, list[Path]] = {}
    for d in sticker_root.iterdir():
        if not d.is_dir():
            continue
        files = [f for f in d.iterdir()
                 if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")]
        if files:
            name_to_files[d.name] = files

    if not name_to_files:
        return text

    def _sticker_repl(m: re.Match) -> str:
        name = m.group(1).strip()
        files = name_to_files.get(name)
        if files:
            chosen = random.choice(files)
            return f"[CQ:image,file=file:///{chosen.as_posix()},subType=1]"
        return ""

    text = re.sub(r'\[动画表情:([^\]]+)\]', _sticker_repl, text)
    text = re.sub(r'\[sticker:([^\]]+)\]', _sticker_repl, text)
    text = re.sub(r'\[贴纸:([^\]]+)\]', _sticker_repl, text)
    return text


# ==================== 消息发送 ====================

async def send_group_msg(ws, group_id: int, message: str):
    payload = {
        "action": "send_group_msg",
        "params": {
            "group_id": group_id,
            "message": message,
        },
    }
    await ws.send(json.dumps(payload))


async def send_private_msg(ws, user_id: int, message: str):
    payload = {
        "action": "send_private_msg",
        "params": {
            "user_id": user_id,
            "message": message,
        },
    }
    await ws.send(json.dumps(payload))


# ==================== @ 解析 ====================

def _resolve_at_mentions(text: str, event: dict) -> str:
    """将 AI 回复中的 @名字 转换为 [CQ:at,qq=数字]"""
    chat_id = config._chat_key(event)
    log = storage.load_chat_log(chat_id)

    name_to_qq: dict[str, str] = {}
    for entry in log:
        uid = entry.get("user_id")
        name = entry.get("nickname", "")
        if uid and name:
            name_to_qq[name] = str(uid)

    def _at_repl(m: re.Match) -> str:
        name = m.group(1)
        qq = name_to_qq.get(name)
        if qq:
            return f"[CQ:at,qq={qq}]"
        return m.group(0)

    text = re.sub(r'@([^\s,，。.!?！？\n\r@\[\]\(\)（）:：]{1,30})', _at_repl, text)
    return text


# ==================== 命令解析 ====================

def parse_command(text: str):
    """解析 /命令 参数，返回 (命令, 参数)"""
    text = text.strip()
    text = re.sub(r'\[CQ:at[^\]]*\]', '', text)
    text = re.sub(r'@\S+\s*', '', text, count=1)
    text = text.strip()
    if not text.startswith("/"):
        return None, None
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return cmd, args


# ==================== QQ 表情映射 ====================

QQ_FACE_MAP: dict[int, str] = {
    0:  "[惊讶]", 1: "[撇嘴]", 2:  "[色]", 3: "[发呆]", 4: "[得意]",
    5:  "[流泪]", 6: "[害羞]", 7:  "[闭嘴]", 8: "[睡]", 9: "[大哭]",
    10: "[尴尬]", 11: "[发怒]", 12: "[调皮]", 13: "[呲牙]", 14: "[微笑]",
    15: "[难过]", 16: "[酷]", 18: "[抓狂]", 19: "[吐]", 20: "[偷笑]",
    21: "[可爱]", 22: "[白眼]", 23: "[傲慢]", 24: "[饥饿]", 25: "[困]",
    26: "[惊恐]", 27: "[流汗]", 28: "[憨笑]", 29: "[悠闲]", 30: "[奋斗]",
    31: "[咒骂]", 32: "[疑问]", 33: "[嘘]", 34: "[晕]", 35: "[折磨]",
    36: "[衰]", 37: "[骷髅]", 38: "[敲打]", 39: "[再见]", 40: "[擦汗]",
    41: "[抠鼻]", 42: "[鼓掌]", 43: "[糗大了]", 44: "[坏笑]", 45: "[左哼哼]",
    46: "[右哼哼]", 47: "[哈欠]", 48: "[鄙视]", 49: "[委屈]", 50: "[快哭了]",
    51: "[阴险]", 52: "[亲亲]", 53: "[吓]", 54: "[可怜]", 55: "[菜刀]",
    56: "[西瓜]", 57: "[啤酒]", 58: "[篮球]", 59: "[乒乓球]", 60: "[咖啡]",
    61: "[饭]", 62: "[猪头]", 63: "[玫瑰]", 64: "[凋谢]", 65: "[示爱]",
    66: "[爱心]", 67: "[心碎]", 68: "[蛋糕]", 69: "[闪电]", 70: "[炸弹]",
    71: "[刀]", 72: "[足球]", 73: "[瓢虫]", 74: "[便便]", 75: "[月亮]",
    76: "[太阳]", 77: "[礼物]", 78: "[拥抱]", 79: "[强]", 80: "[弱]",
    81: "[握手]", 82: "[胜利]", 83: "[抱拳]", 84: "[勾引]", 85: "[拳头]",
    86: "[差劲]", 87: "[爱你]", 88: "[NO]", 89: "[OK]", 90: "[爱情]",
    91: "[飞吻]", 92: "[跳跳]", 93: "[发抖]", 94: "[怄火]", 95: "[转圈]",
    96: "[磕头]", 97: "[回头]", 98: "[跳绳]", 99: "[挥手]", 100: "[激动]",
    101: "[街舞]", 102: "[献吻]", 103: "[左太极]", 104: "[右太极]",
    105: "[双喜]", 106: "[鞭炮]", 107: "[灯笼]", 108: "[发财]",
    109: "[K歌]", 110: "[购物]", 111: "[邮件]", 112: "[帅]", 113: "[喝彩]",
    114: "[祈祷]", 115: "[爆筋]", 116: "[棒棒糖]", 117: "[喝奶]",
    118: "[下面]", 119: "[香蕉]", 120: "[飞机]", 121: "[开车]",
    122: "[高铁左车头]", 123: "[车厢]", 124: "[高铁右车头]", 125: "[多云]",
    126: "[下雨]", 127: "[钞票]", 128: "[熊猫]", 129: "[灯泡]",
    130: "[风车]", 131: "[闹钟]", 132: "[雨伞]", 133: "[彩球]",
    134: "[钻戒]", 135: "[沙发]", 136: "[纸巾]", 137: "[药]", 138: "[手枪]",
    139: "[青蛙]", 140: "[茶]", 141: "[眨眼睛]", 142: "[泪奔]", 143: "[无奈]",
    144: "[卖萌]", 145: "[小纠结]", 146: "[喷血]", 147: "[斜眼]",
    148: "[doge]", 149: "[惊喜]", 150: "[骚扰]", 151: "[笑哭]",
    152: "[我最美]", 153: "[河蟹]", 154: "[羊驼]", 155: "[幽灵]",
    156: "[蛋]", 157: "[菊花]", 158: "[红包]", 159: "[大笑]", 160: "[不开心]",
    172: "[点赞]", 173: "[耶]", 174: "[cp]", 175: "[跳跳]", 176: "[发抖]",
    177: "[怄火]", 178: "[转圈]", 179: "[挥手]", 180: "[回头]", 181: "[跳绳]",
    182: "[激动]", 183: "[街舞]", 184: "[献吻]", 185: "[左太极]", 186: "[右太极]",
    187: "[双喜]", 188: "[鞭炮]", 189: "[灯笼]", 190: "[发财]", 191: "[K歌]",
    192: "[购物]", 193: "[邮件]", 194: "[帅]", 195: "[喝彩]", 196: "[祈祷]",
    197: "[爆筋]", 198: "[棒棒糖]", 199: "[喝奶]", 200: "[下面]", 201: "[香蕉]",
    202: "[飞机]", 203: "[开车]", 204: "[左车头]", 205: "[车厢]", 206: "[右车头]",
    207: "[多云]", 208: "[下雨]", 209: "[钞票]", 210: "[熊猫]", 211: "[灯泡]",
    212: "[风车]", 213: "[闹钟]", 214: "[雨伞]", 215: "[彩球]", 216: "[钻戒]",
    217: "[沙发]", 218: "[纸巾]", 219: "[药]", 220: "[手枪]", 221: "[青蛙]",
    222: "[茶]", 223: "[眨眼]", 224: "[泪奔]", 225: "[无奈]", 226: "[卖萌]",
    227: "[小纠结]", 228: "[喷血]", 229: "[斜眼]", 230: "[doge]", 231: "[惊喜]",
    232: "[骚扰]", 233: "[笑哭]", 234: "[我最美]", 235: "[河蟹]", 236: "[羊驼]",
    237: "[幽灵]", 238: "[蛋]", 239: "[菊花]", 240: "[红包]", 241: "[大笑]",
    242: "[不开心]", 243: "[冷漠]", 244: "[呃]", 245: "[好棒]", 246: "[拜托]",
    247: "[点赞]", 248: "[无聊]", 249: "[托脸]", 250: "[吃]", 251: "[送花]",
    252: "[害怕]", 253: "[花痴]", 254: "[小样儿]", 255: "[土豪]", 256: "[喵喵]",
    257: "[受惊]", 258: "[小黄脸惊讶]", 259: "[戴口罩]", 260: "[藏狐]",
    261: "[狗狗]", 262: "[金牙]", 263: "[汗]", 264: "[即墨]", 265: "[抠鼻]",
}


# ==================== 分享 / B站 工具 ====================

def _fmt_json_share(data: dict) -> str:
    parts = []
    meta = data.get("meta", {})
    detail = (meta.get("detail_1") or meta.get("detail") or {})
    desc = detail.get("desc", "")
    qqdocurl = detail.get("qqdocurl", "")
    prompt = data.get("prompt", "")
    if desc:
        parts.append(desc)
    elif prompt:
        parts.append(prompt)
    if qqdocurl:
        qqdocurl = unquote(qqdocurl)
        parts.append(qqdocurl)
    if parts:
        return f"[分享: {' | '.join(parts)}]"
    return "[分享]"


_BV_RE = re.compile(r'https?://(?:www\.)?bilibili\.com/video/(BV[a-zA-Z0-9]{10})(?:\?[^\s\]\)]*)?')
_B23_RE = re.compile(r'https?://b23\.tv/([a-zA-Z0-9]+)(?:\?[^\s\]\)]*)?')
_AV_RE = re.compile(r'https?://(?:www\.)?bilibili\.com/video/av(\d+)(?:\?[^\s\]\)]*)?', re.IGNORECASE)

_b23_cache: dict[str, str | None] = {}


async def _resolve_b23(short_code: str) -> str | None:
    """解析 b23.tv 短链，返回 'BVxxxxxx' 或 'av12345'，失败返回 None"""
    if short_code in _b23_cache:
        return _b23_cache[short_code]

    url = f"https://b23.tv/{short_code}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                final_url = str(resp.url)
    except Exception:
        _b23_cache[short_code] = None
        return None

    # 提取 BV 或 AV 号
    m = _BV_RE.search(final_url) or _AV_RE.search(final_url)
    if m:
        result = m.group(0) if m.re is _BV_RE else "av" + m.group(1)
        # 从完整 URL 中提取：_BV_RE group(0) 是整个 bilibili.com/video/BVxxx
        if m.re is _BV_RE:
            result = m.group(1)
        _b23_cache[short_code] = result
        return result

    _b23_cache[short_code] = None
    return None


async def _replace_bilibili_urls(text: str) -> str:
    """把消息中的 B站链接原地替换成 BV/AV 标识，省 token"""
    # 1. 处理完整 bilibili.com/video/ 链接（捕获组已含 BV/av 前缀）
    text = _BV_RE.sub(r'B站:\1', text)
    text = _AV_RE.sub(r'B站:av\1', text)

    # 2. 处理 b23.tv 短链（需要解析）
    def _b23_replacer(m: re.Match) -> str:
        resolved = _b23_cache.get(m.group(1))
        if resolved:
            return f"B站:{resolved}"
        return m.group(1)  # 解析失败保留原始短码

    b23_matches = list(_B23_RE.finditer(text))
    if b23_matches:
        # 并发解析所有短链
        codes = [m.group(1) for m in b23_matches]
        for code in codes:
            if code not in _b23_cache:
                await _resolve_b23(code)
        text = _B23_RE.sub(_b23_replacer, text)

    return text


# ==================== CQ 码字符串 → segments 数组 ====================

def _parse_cq_params(s: str) -> dict[str, str]:
    """解析 CQ 码参数字符串 'key1=val1,key2=val2' → dict。
    OneBot 规范：值中的特殊字符用 &#44;(逗号) &#91;([) &#93;(]) 转义。
    """
    result: dict[str, str] = {}
    for m in re.finditer(r'(\w+)=', s):
        key = m.group(1)
        val_start = m.end()
        # 值结束于：下一条 key= 或字符串末尾
        next_key = re.search(r',\s*\w+=', s[val_start:])
        if next_key:
            val = s[val_start:val_start + next_key.start()]
        else:
            val = s[val_start:]
        val = val.strip().rstrip(',')
        # 解码 HTML 实体 + URL 编码
        val = val.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")
        try:
            val = unquote(val)
        except Exception:
            pass
        result[key] = val
    return result


_CQ_RE = re.compile(r'\[CQ:(\w+)(?:,([^\]]*))?\]')

def _find_cq_end(text: str, start: int) -> int:
    """括号深度匹配：从 [CQ: 起始位置找到配对的 ]（处理 JSON 内嵌 [... ]）。"""
    depth = 0
    in_quote = False
    quote_char = ''
    k = start
    while k < len(text):
        c = text[k]
        if in_quote:
            if c == '\\':
                k += 2  # 跳过转义字符和它后面的字符
                continue
            elif c == quote_char:
                in_quote = False
        else:
            if c in ('"', "'"):
                in_quote = True
                quote_char = c
            elif c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    return k
        k += 1
    return -1


def _cq_string_to_segments(text: str) -> list[dict]:
    """将 CQ 码字符串拆成 segments 数组，与 WebSocket array 格式统一。
    用括号深度匹配替代正则，正确处理 [CQ:json,data={...[...]...}]。
    """
    segments: list[dict] = []
    i = 0
    n = len(text)

    while i < n:
        cq_start = text.find('[CQ:', i)
        if cq_start == -1:
            if i < n:
                segments.append({"type": "text", "data": {"text": text[i:]}})
            break

        if cq_start > i:
            segments.append({"type": "text", "data": {"text": text[i:cq_start]}})

        cq_end = _find_cq_end(text, cq_start)
        if cq_end == -1:
            segments.append({"type": "text", "data": {"text": text[cq_start:]}})
            break

        cq_content = text[cq_start + 1:cq_end]  # 去掉 [... ]
        if cq_content.startswith('CQ:'):
            cq_content = cq_content[3:]  # 去掉 'CQ:'
        comma = cq_content.find(',')
        if comma != -1:
            cq_type = cq_content[:comma]
            params_str = cq_content[comma + 1:]
        else:
            cq_type = cq_content
            params_str = ""

        data = _parse_cq_params(params_str)
        segments.append({"type": cq_type, "data": data})
        i = cq_end + 1

    return segments


# ==================== 消息段数组 → 纯文本 ====================

async def _download_image_bytes(url: str) -> bytes | None:
    """下载图片，超过 5MB 或失败返回 None。"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if len(data) > 5 * 1024 * 1024:
                    return None
                return data
    except Exception:
        return None


async def _msg_segments_to_text(segments: list, qq_to_name: dict[str, str] | None = None,
                                chat_id: str | None = None, ws=None,
                                image_sink: list | None = None) -> str:
    """将 OneBot message array 格式转为纯文本。

    reply 段串行查找：data.text → 本地日志(id) → API(ws) → [回复]
    image_sink 不为 None 时：图片落盘并收集元数据，文本里只留 [图片]/[动画表情] 占位符。
    """
    parts = []
    img_recognized = 0  # 本地模型识别计数（最多 2 张）
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        data = seg.get("data", {})
        if t == "text":
            parts.append(data.get("text", ""))
        elif t == "face":
            face_id = int(data.get("id", 0))
            raw_info = data.get("raw", {})
            if isinstance(raw_info, dict):
                ft = raw_info.get("faceText", "")
                if ft:
                    parts.append(ft)
                    continue
            parts.append(QQ_FACE_MAP.get(face_id, f"[表情:{face_id}]"))
        elif t == "image":
            summary = data.get("summary", "")
            sub = str(data.get("sub_type") or data.get("subType", "0"))
            is_sticker = sub == "1"
            label = "[动画表情]" if is_sticker else "[图片]"
            url = data.get("url", "") or data.get("file", "")

            if image_sink is not None:
                # 原生多模态：图片落盘并保留带编号的占位符，不转文字描述
                idx = len(image_sink)
                meta = None
                if url:
                    raw = await _download_image_bytes(url)
                    if raw:
                        meta = media_store.encode_frames(raw, is_sticker=is_sticker, chat_id=chat_id)
                if meta is None and summary:
                    # 落盘失败但有客户端摘要 → 用文字，不占图片槽位
                    parts.append(f"[{label[1:-1]}：{summary}]")
                else:
                    if meta is None:
                        meta = {"files": [], "tier": "none"}
                    meta["id"] = idx
                    image_sink.append(meta)
                    parts.append(f"[{label[1:-1]}#{idx}]")
            elif config.VISION_NATIVE_IN_CHAT:
                # 原生模式下没有 sink（如引用消息）只留占位符，不触发本地模型
                parts.append(f"[{label[1:-1]}：{summary}]" if summary else label)
            elif summary:
                parts.append(f"[{label[1:-1]}：{summary}]")
            elif img_recognized < 3 and url:
                img_recognized += 1
                desc = await vision._describe_image_url(url, is_sticker=is_sticker)
                parts.append(f"[{label[1:-1]}：可能是{desc}]" if desc else label)
            else:
                parts.append(label)
        elif t == "record":
            parts.append("[语音]")
        elif t == "at":
            qq = data.get("qq", "")
            name = data.get("name") or data.get("display") or ""
            if name:
                parts.append(f"@{name}")
            elif qq and qq_to_name and qq in qq_to_name:
                parts.append(f"@{qq_to_name[qq]}")
            elif qq:
                parts.append(f"@{qq}")
        elif t == "reply":
            quoted = data.get("text") or data.get("message") or ""
            sender = (data.get("sender_name") or data.get("sender_nickname")
                      or data.get("nickname") or data.get("user_id")
                      or data.get("qq") or "")
            msg_id = data.get("id", "")

            # ① 有 text → 直接用
            # ② 无 text → 本地日志按 id 查
            if not quoted and chat_id and msg_id:
                log = storage.load_chat_log(chat_id)
                for entry in reversed(log):
                    if str(entry.get("message_id")) == str(msg_id):
                        quoted = entry.get("message", "")
                        if not sender:
                            sender = entry.get("nickname", "")
                        break

            # ③ 本地没有 → API 查询
            if not quoted and ws and msg_id:
                try:
                    resp = await storage._api_call(ws, "get_msg", {"message_id": int(msg_id)})
                    if resp.get("status") == "ok":
                        api_data = resp.get("data", {})
                        api_sender = api_data.get("sender", {})
                        quoted, _, _ = await extract_text(
                            {"message": api_data.get("message", ""),
                             "raw_message": api_data.get("raw_message", ""),
                             "message_type": "group", "group_id": 0},
                            ws=ws, collect_images=False,
                        )
                        if not sender:
                            sender = api_sender.get("card") or api_sender.get("nickname") or ""
                except Exception:
                    pass

            if quoted:
                quote_preview = quoted.replace("\n", " ")
                if len(quote_preview) > 50:
                    quote_preview = quote_preview[:50] + "…"
                label = f"[回复{sender}: {quote_preview}]" if sender else f"[回复: {quote_preview}]"
                parts.append(label)
            else:
                parts.append("[回复]")
        elif t == "json":
            json_str = data.get("data", "")
            if json_str:
                try:
                    obj = json.loads(json_str)
                    parts.append(_fmt_json_share(obj))
                except Exception:
                    parts.append("[分享]")
            else:
                parts.append("[分享]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "file":
            parts.append(f"[文件: {data.get('name', '')}]")
        else:
            parts.append("")
    return "".join(parts)


# ==================== 文本提取 ====================

async def extract_text(event: dict, qq_to_name: dict[str, str] | None = None,
                       ws=None, collect_images: bool = True):
    """从事件中提取纯文本，返回 (完整文字, 去空白文字, 图片元数据列表)。

    string → segments → 逐段解析（图片落盘/识别、回复串行查找）→ BV 追加
    """
    raw = event.get("message") or event.get("raw_message") or ""
    if isinstance(raw, list):
        segments = raw
    else:
        segments = _cq_string_to_segments(str(raw))
    chat_id = config._chat_key(event)

    sink: list | None = None
    if collect_images and config.VISION_NATIVE_IN_CHAT:
        sink = []
    text = await _msg_segments_to_text(segments, qq_to_name, chat_id, ws, image_sink=sink)

    # 替换 B站链接为标识符
    text = await _replace_bilibili_urls(text)

    clean = text.strip()
    return text, clean, (sink or [])


# ==================== 文本工具 ====================

def _split_long_text(text: str, max_len: int = 150) -> list[str]:
    """将长文本按中文标点拆成短句序列，表情包标签独立成段"""
    text = re.sub(r'\n{2,}', '\n', text.strip())
    if not text:
        return []

    # 先把 [动画表情:xxx] / [sticker:xxx] / [贴纸:xxx] 拆成独立段
    raw_parts = re.split(r'(\[(?:动画表情|sticker|贴纸):[^\]]+\])', text)
    result: list[str] = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        # 是表情包标签 → 直接作为一段
        if re.match(r'^\[(?:动画表情|sticker|贴纸):[^\]]+\]$', part):
            result.append(part)
            continue
        # 普通文字 → 走原有分句逻辑
        if len(part) <= max_len and len(part) <= 20:
            s = re.sub(r'[。；]+$', '', part)
            if s:
                result.append(s)
            continue
        result.extend(_split_plain_text(part, max_len))
    return result


def _split_plain_text(text: str, max_len: int) -> list[str]:
    """将纯文字按中文标点拆成短句（内部函数）"""
    result = []
    buf = ""
    i = 0
    n = len(text)

    def _emit():
        nonlocal buf
        s = re.sub(r'[。；]+$', '', buf)
        s = s.strip()
        if s:
            result.append(s)
        buf = ""

    while i < n:
        ch = text[i]

        if ch == '—' and i + 1 < n and text[i + 1] == '—':
            buf += '——'
            i += 2
            _emit()
            continue

        if ch == '\n':
            i += 1
            _emit()
            continue

        if ch.lower() == 'w':
            j = i
            while j < n and text[j].lower() == 'w':
                j += 1
            w_count = j - i
            if w_count >= 3:
                buf += text[i:j]
                i = j
                _emit()
                continue

        if ch == '…' and i + 1 < n and text[i+1] == '…' and len(buf) > 20:
            buf += '……'
            i += 2
            _emit()
            continue

        if ch in '！？' and len(buf) > 20:
            j = i
            while j < n and text[j] in '！？':
                j += 1
            buf += text[i:j]
            i = j
            _emit()
            continue

        if ch in '。；' and len(buf) > 20:
            buf += ch
            i += 1
            _emit()
            continue

        buf += ch
        i += 1

    _emit()
    return result if result else [text]


_STICKER_RE = re.compile(r'^\[(?:动画表情|sticker|贴纸|CQ):[^\]]+\]$')


def _merge_overflow_segments(segments: list[str], max_segments: int) -> list[str]:
    """段数超限时随机合并相邻纯文字段，每段最多合并一次，直到文字段数 ≤ max_segments。
    表情包/CQ段不计入限制，也不参与合并。"""
    import random as _random

    result = list(segments)
    # 每个位置是否已经参与过合并（和 result 保持同步）
    merged: list[bool] = [False] * len(result)

    while True:
        text_idx = [i for i, s in enumerate(result) if not _STICKER_RE.match(s)]
        if len(text_idx) <= max_segments:
            break

        # 找相邻且都未参与过合并的纯文字对
        pairs = []
        for j in range(len(text_idx) - 1):
            a, b = text_idx[j], text_idx[j + 1]
            if b == a + 1 and not merged[a] and not merged[b]:
                pairs.append(a)

        if not pairs:
            break

        _random.shuffle(pairs)
        i = pairs[0]
        result[i] = result[i] + "\n" + result[i + 1]
        result.pop(i + 1)
        # merged 同步：合并后标记为已合并，pop 对齐
        merged[i] = True
        merged.pop(i + 1)

    return result

