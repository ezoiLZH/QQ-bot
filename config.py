"""
Bot 配置模块 —— 路径、运行时参数、BotConfig/BotInstance
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass


VERSION = "2.1"


# ==================== 基础工具 ====================

def _chat_key(event: dict) -> str:
    """统一会话标识。群聊用 group-{id}，私聊用 private-{bot_qq}-{user_id}（不同 bot 独立）"""
    msg_type = event.get("message_type", "")
    if msg_type == "group":
        return f"group-{event.get('group_id')}"
    else:
        self_id = event.get("self_id", "")
        return f"private-{self_id}-{event.get('user_id')}"


# ==================== 共享状态（多 bot 跨模块访问）====================
_ALL_BOTS: list = []          # BotInstance 列表
_BOT_QQS: set[str] = set()    # 快速判断 sender 是否为 bot
_heavy_task_active: bool = False  # 长耗时指令期间暂停 AI 回复


def _find_bot_by_ws(ws) -> "BotInstance | None":
    """根据 WebSocket 连接查找对应的 BotInstance"""
    for b in _ALL_BOTS:
        if b.ws is ws:
            return b
    return None


def _targeted_bots(event: dict) -> "list[BotInstance]":
    """返回消息 @ 了哪些 bot（群聊）或发给了哪个 bot（私聊）"""
    # 私聊：self_id 就是目标 bot
    if event.get("message_type") != "group":
        self_id = str(event.get("self_id", ""))
        return [b for b in _ALL_BOTS if b.cfg.bot_qq == self_id]

    # 群聊：从 message array 中提取被 @ 的 bot
    msg = event.get("message", [])
    if not isinstance(msg, list):
        msg = []
    at_qqs: set[str] = set()
    for seg in msg:
        if seg.get("type") == "at":
            qq = str(seg.get("data", {}).get("qq", ""))
            if qq:
                at_qqs.add(qq)
    if not at_qqs:
        return []
    return [b for b in _ALL_BOTS if b.cfg.bot_qq in at_qqs]


# ==================== 路径 & 配置 ====================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.yaml"
BOTS_FILE = BASE_DIR / "bots.yaml"        # 多 bot 注册（独立于全局配置）
CURSOR_FILE = DATA_DIR / "_cursors.json"  # 持久化 session cursor，重启恢复


def _load_config() -> dict:
    """加载 config.yaml，不存在则用默认值"""
    try:
        import yaml
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"⚠️ 加载 config.yaml 失败: {e}")
    return {}


_config: dict = {}

# ── 可热重载的运行时参数 ──
# 这些值由 _apply_config() 设置，/reload 可重新读取 config.yaml 更新
WS_URL: str = "ws://127.0.0.1:3001"
ACCESS_TOKEN: str = ""
CLAUDE_MAX_TURNS: int = 10
CLAUDE_TIMEOUT: int = 120
CLAUDE_MODEL: str = "deepseek-flash[1M]"
CLAUDE_BASE_URL: str = "https://api.deepseek.com/anthropic"
CLAUDE_MEMORY_MODEL: str = "deepseek-flash[1M]"  # 记忆总结专用（现统一使用 deepseek-flash）
CLAUDE_MEMORY_TIMEOUT: int = 300     # 记忆总结超时（秒）
CLAUDE_API_KEY: str = ""    # 空则继承父进程环境变量


def claude_settings_json(api_key: str = "") -> str | None:
    """生成传给 CLI --settings 的 JSON（--setting-sources 为空时仍生效）。

    只包含 base_url 和显式配置的 api_key；未显式配置 key 时沿用子进程继承的
    ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY，不把环境里的 key 写进命令行。
    """
    env: dict[str, str] = {}
    if CLAUDE_BASE_URL:
        env["ANTHROPIC_BASE_URL"] = CLAUDE_BASE_URL
    if api_key:
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
    return json.dumps({"env": env}, ensure_ascii=False) if env else None



VISION_ENABLED: bool = True          # 是否启用视觉模型
VISION_FORCE_CPU: bool = False       # 强制 CPU 推理（即使有 CUDA）
VISION_MODEL_ID: str = str(BASE_DIR / "models" / "Qwen2-VL-2B-Instruct")
VISION_MAX_AREA: int = 1024 * 1024
VISION_TEMPERATURE: float = 0.2
VISION_MAX_FRAMES: int = 3          # 动图（GIF/WebP）最多抽取的帧数
VISION_NATIVE_IN_CHAT: bool = True   # 图片以原生多模态进入主对话（不转文字描述）
VISION_ADAPTIVE_RESOLUTION: bool = True  # 按图片信息量自动选择分辨率
VISION_RES_SIMPLE: int = 512         # 简单图（扁平梗图/表情）长边上限
VISION_RES_NORMAL: int = 768         # 普通图长边上限
VISION_RES_DENSE: int = 1024         # 密集图（文字截图等）长边上限
VISION_IMAGE_CLEANUP_GRACE: int = 600      # 图片清理保护期（秒），避免误删刚收到的图
MAX_HISTORY: int = 30
MAX_CONTEXT_CHARS: int = 150
SESSION_SOFT_LIMIT: int = 50    # 空闲超过 SESSION_IDLE_SECONDS 且超此条数 → compact
SESSION_HARD_LIMIT: int = 200   # 无论是否活跃，超过此条数 → compact
SESSION_IDLE_SECONDS: int = 300 # 5 分钟没触发回复 → 判定空闲
SESSION_COMPACT_TO: int = 10    # compact 后保留条数
SESSION_OVERLAP: int = 5         # compact 时重叠缓冲条数（切除和保留的过渡区）
LOG_MAX_ENTRIES: int = 250
MAX_SEGMENTS: int = 5
SKIP_COOLDOWN_REPLY: int = 5
SKIP_COOLDOWN_1: int = 15
SKIP_COOLDOWN_2: int = 30
SKIP_COOLDOWN_3: int = 60
SKIP_COOLDOWN_5: int = 60  # 连续 5+ 次 SKIP 冷却（秒）
LEAVE_COOLDOWN: int = 600  # [LEAVE] 暂时离开时长（秒）
MEMORY_MAX_ENTRIES: int = 20    # 长期记忆最多保留条数
MEMORY_MIN_COMPACT: int = 15    # compact 至少移除这么多条才触发总结
MAX_REPLY_LENGTH: int = 800
REPLY_COOLDOWN: int = 10  # _chat_monitor idle_timeout 计算
REPLY_SEGMENT_DELAY_BASE: float = 2.0
REPLY_SEGMENT_DELAY_PER_CHAR: float = 0.1
REPLY_SEGMENT_DELAY_CHAR_THRESHOLD: int = 10
# 分层冷却：根据回复频率自动升级冷却时间
RATE_TIER1_COUNT: int = 3
RATE_TIER1_WINDOW: int = 60
RATE_TIER1_FACTOR: float = 1.5
RATE_TIER2_COUNT: int = 10
RATE_TIER2_WINDOW: int = 300
RATE_TIER2_FACTOR: float = 2.0
RATE_CAP_COUNT: int = 15
RATE_CAP_WINDOW: int = 600
STICKER_MAX_AREA: int = 512 * 512
_GROUP_MEMBER_TTL: int = 600
BLACKLIST_GROUPS: list[str] = []
AGGREGATION_WINDOW_TIMEOUT: float = 5.0
AGGREGATION_MAX_MESSAGES: int = 5
# AI 状态已移入 BotInstance（per-bot）

# 查找 claude 可执行文件
import shutil
_claude_exe = "claude"
# 优先用底层 .exe，避免 Windows .cmd 转发导致的参数解析问题
for _p in [
    "D:/ProgramData/nodejs/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    "claude.exe",
    "claude",
    "claude.cmd",
]:
    if shutil.which(_p):
        _claude_exe = _p
        break

# Session resume template（供 BotInstance 复制）
_SESSION_TEMPLATE_ID = "00000000-0000-4000-b000-000000000001"
_EMPTY_SESSION_TEMPLATE = BASE_DIR / "sessions" / f"{_SESSION_TEMPLATE_ID}.jsonl.empty.v2"

# Claude Code 的 projects 目录名由 cwd 推导（非字母数字替换为 -）
_CLAUDE_PROJECT_DIR = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9_-]", "-", str(BASE_DIR))

# 记忆总结专用 session（独立缓存前缀）
_MEM_SESSION_ID = "00000000-0000-4000-a000-000000000001"
_MEM_SESSION_FILE = _CLAUDE_PROJECT_DIR / f"{_MEM_SESSION_ID}.jsonl"
_MEM_EMPTY_SESSION = BASE_DIR / "sessions" / f"{_MEM_SESSION_ID}.jsonl.empty.v2"


def _apply_config(cfg: dict):
    """将 config.yaml 字典应用到模块级全局变量。启动时调用，/reload 也可调用。"""
    global _config, WS_URL, ACCESS_TOKEN
    global CLAUDE_MAX_TURNS, CLAUDE_TIMEOUT, CLAUDE_MODEL, CLAUDE_BASE_URL, CLAUDE_MEMORY_MODEL, CLAUDE_MEMORY_TIMEOUT, CLAUDE_API_KEY
    global VISION_ENABLED, VISION_FORCE_CPU, VISION_MODEL_ID, VISION_MAX_AREA, VISION_TEMPERATURE, VISION_MAX_FRAMES
    global VISION_NATIVE_IN_CHAT, VISION_ADAPTIVE_RESOLUTION, VISION_RES_SIMPLE, VISION_RES_NORMAL, VISION_RES_DENSE
    global VISION_IMAGE_CLEANUP_GRACE
    global MAX_HISTORY, MAX_CONTEXT_CHARS, MAX_REPLY_LENGTH, REPLY_COOLDOWN
    global REPLY_SEGMENT_DELAY_BASE, REPLY_SEGMENT_DELAY_PER_CHAR, REPLY_SEGMENT_DELAY_CHAR_THRESHOLD
    global SESSION_SOFT_LIMIT, SESSION_HARD_LIMIT, SESSION_IDLE_SECONDS, SESSION_COMPACT_TO
    global SESSION_OVERLAP
    global LOG_MAX_ENTRIES, MAX_SEGMENTS
    global SKIP_COOLDOWN_REPLY, SKIP_COOLDOWN_1, SKIP_COOLDOWN_2, SKIP_COOLDOWN_3, SKIP_COOLDOWN_5
    global LEAVE_COOLDOWN
    global MEMORY_MAX_ENTRIES, MEMORY_MIN_COMPACT
    global RATE_TIER1_COUNT, RATE_TIER1_WINDOW, RATE_TIER1_FACTOR
    global RATE_TIER2_COUNT, RATE_TIER2_WINDOW, RATE_TIER2_FACTOR
    global RATE_CAP_COUNT, RATE_CAP_WINDOW
    global STICKER_MAX_AREA, _GROUP_MEMBER_TTL, BLACKLIST_GROUPS
    global AGGREGATION_WINDOW_TIMEOUT, AGGREGATION_MAX_MESSAGES

    _config = cfg

    _cfg_ws = cfg.get("ws", {})
    WS_URL = _cfg_ws.get("url", "ws://127.0.0.1:3001")
    ACCESS_TOKEN = _cfg_ws.get("access_token", "")

    _cfg_claude = cfg.get("claude", {})
    CLAUDE_MAX_TURNS = _cfg_claude.get("max_turns", 10)
    CLAUDE_TIMEOUT = _cfg_claude.get("timeout", 120)
    CLAUDE_MODEL = _cfg_claude.get("model", "deepseek-flash[1M]")
    CLAUDE_BASE_URL = (
        _cfg_claude.get("base_url", "")
        or os.getenv("ANTHROPIC_BASE_URL", "")
        or "https://api.deepseek.com/anthropic"
    )
    CLAUDE_MEMORY_MODEL = _cfg_claude.get("memory_model", "deepseek-flash[1M]")
    CLAUDE_MEMORY_TIMEOUT = _cfg_claude.get("memory_timeout", 300)
    CLAUDE_API_KEY = _cfg_claude.get("api_key", "")  # 空=继承父进程

    _cfg_vision = cfg.get("vision", {})
    VISION_ENABLED = _cfg_vision.get("enabled", True)
    VISION_FORCE_CPU = _cfg_vision.get("force_cpu", False)
    VISION_MODEL_ID = _cfg_vision.get("model_id", str(BASE_DIR / "models" / "Qwen2-VL-2B-Instruct"))
    VISION_MAX_AREA = _cfg_vision.get("max_area", 1024 * 1024)
    VISION_TEMPERATURE = float(_cfg_vision.get("temperature", 0.2))
    VISION_MAX_FRAMES = int(_cfg_vision.get("max_frames", 3))
    VISION_NATIVE_IN_CHAT = bool(_cfg_vision.get("native_in_chat", True))
    VISION_ADAPTIVE_RESOLUTION = bool(_cfg_vision.get("adaptive_resolution", True))
    VISION_RES_SIMPLE = int(_cfg_vision.get("resolution_simple", 512))
    VISION_RES_NORMAL = int(_cfg_vision.get("resolution_normal", 768))
    VISION_RES_DENSE = int(_cfg_vision.get("resolution_dense", 1024))
    VISION_IMAGE_CLEANUP_GRACE = int(_cfg_vision.get("image_cleanup_grace", 600))

    _cfg_chat = cfg.get("chat", {})
    MAX_HISTORY = _cfg_chat.get("max_history", 30)
    MAX_CONTEXT_CHARS = _cfg_chat.get("max_context_chars", 150)
    MAX_REPLY_LENGTH = _cfg_chat.get("max_reply_length", 800)
    REPLY_COOLDOWN = _cfg_chat.get("reply_cooldown", 10)
    REPLY_SEGMENT_DELAY_BASE = _cfg_chat.get("segment_delay_base", 2.0)
    REPLY_SEGMENT_DELAY_PER_CHAR = _cfg_chat.get("segment_delay_per_char", 0.1)
    REPLY_SEGMENT_DELAY_CHAR_THRESHOLD = _cfg_chat.get("segment_delay_char_threshold", 10)
    # 增长式会话
    global SESSION_SOFT_LIMIT, SESSION_HARD_LIMIT, SESSION_IDLE_SECONDS, SESSION_COMPACT_TO
    global SESSION_OVERLAP, LOG_MAX_ENTRIES
    SESSION_SOFT_LIMIT = _cfg_chat.get("session_soft_limit", 50)
    SESSION_HARD_LIMIT = _cfg_chat.get("session_hard_limit", 200)
    SESSION_IDLE_SECONDS = _cfg_chat.get("session_idle_seconds", 300)
    SESSION_COMPACT_TO = _cfg_chat.get("session_compact_to", 10)
    SESSION_OVERLAP = _cfg_chat.get("session_overlap", 5)
    LOG_MAX_ENTRIES = _cfg_chat.get("log_max_entries", 250)
    MAX_SEGMENTS = _cfg_chat.get("max_segments", 5)
    SKIP_COOLDOWN_REPLY = _cfg_chat.get("skip_cooldown_reply", 5)
    SKIP_COOLDOWN_1 = _cfg_chat.get("skip_cooldown_1", 15)
    SKIP_COOLDOWN_2 = _cfg_chat.get("skip_cooldown_2", 30)
    SKIP_COOLDOWN_3 = _cfg_chat.get("skip_cooldown_3", 60)
    SKIP_COOLDOWN_5 = _cfg_chat.get("skip_cooldown_5", 60)
    LEAVE_COOLDOWN = _cfg_chat.get("leave_cooldown", 600)
    MEMORY_MAX_ENTRIES = _cfg_chat.get("memory_max_entries", 20)
    MEMORY_MIN_COMPACT = _cfg_chat.get("memory_min_compact", 15)
    # 分层冷却
    _cfg_rate = cfg.get("rate_limit", {})
    RATE_TIER1_COUNT = _cfg_rate.get("tier1_count", 3)
    RATE_TIER1_WINDOW = _cfg_rate.get("tier1_window", 60)
    RATE_TIER1_FACTOR = _cfg_rate.get("tier1_factor", 1.5)
    RATE_TIER2_COUNT = _cfg_rate.get("tier2_count", 10)
    RATE_TIER2_WINDOW = _cfg_rate.get("tier2_window", 300)
    RATE_TIER2_FACTOR = _cfg_rate.get("tier2_factor", 2.0)
    RATE_CAP_COUNT = _cfg_rate.get("cap_count", 15)
    RATE_CAP_WINDOW = _cfg_rate.get("cap_window", 600)

    _cfg_stickers = cfg.get("stickers", {})
    STICKER_MAX_AREA = _cfg_stickers.get("max_area", 512 * 512)

    _cfg_group = cfg.get("group", {})
    _GROUP_MEMBER_TTL = _cfg_group.get("member_cache_ttl", 600)

    _cfg_blacklist = cfg.get("blacklist", {})
    BLACKLIST_GROUPS = [str(g) for g in _cfg_blacklist.get("groups", [])]

    _cfg_agg = cfg.get("aggregation", {})
    AGGREGATION_WINDOW_TIMEOUT = _cfg_agg.get("window_timeout", 5.0)
    AGGREGATION_MAX_MESSAGES = _cfg_agg.get("max_messages", 5)


# 不再在模块加载时调用 _apply_config，由 main() 负责


# ==================== Bot 配置 & 实例 ====================

@dataclass
class BotConfig:
    """单个 bot 的静态配置（从 config.yaml 加载）"""
    bot_qq: str
    bot_name: str
    persona: str
    ws_url: str
    access_token: str
    claude_model: str = "deepseek-flash[1M]"
    claude_max_turns: int = 10
    claude_timeout: int = 120
    api_key: str = ""
    admins: list[str] = None  # type: ignore  # 管理员 QQ 号

    def __post_init__(self):
        if self.admins is None:
            self.admins = []


class BotInstance:
    """单个 bot 的运行时状态：连接、人设、冷却、监控"""
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.ws = None
        self.persona_text = ""
        # 独立监控（per-bot per-chat）
        self.chat_has_new: dict[str, int] = {}
        self.events: dict[str, asyncio.Event] = {}
        self.chat_busy: set[str] = set()
        self.spawn_lock = asyncio.Lock()
        self.claude_lock = asyncio.Lock()  # 防止同 bot 并发调用时 session 文件互相覆盖
        # 独立冷却（per-bot per-chat）
        self.reply_history: dict[str, list[float]] = {}
        self.skip_history: dict[str, list[bool]] = {}
        self.last_reply_time: dict[str, float] = {}
        self.leave_until: dict[str, float] = {}  # chat_id → 离开结束时间戳，期间不回复
        self.mentioned_in: set[str] = set()  # 本轮消息中被 @ 的 chat_id，仅用于打断 LEAVE
        # AI 开关
        self.ai_enabled: bool = True
        self.ai_override: bool | None = None
        self.chat_ai_disabled: set[str] = set()
        self.heavy_task_active: bool = False
        # Claude session（独立缓存前缀）
        tail = f"{int(cfg.bot_qq):012d}"
        self.session_id = f"00000000-0000-4000-b000-{tail}"
        self.empty_session = BASE_DIR / "sessions" / f"{self.session_id}.jsonl.empty.v2"
        self.session_file = _CLAUDE_PROJECT_DIR / f"{self.session_id}.jsonl"
        self._load_persona()

    def _load_persona(self):
        path = BASE_DIR / "persona" / f"{self.cfg.persona}.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    self.persona_text = parts[2].lstrip("\n")
                else:
                    print(f"  ⚠️ [{self.cfg.bot_name}] persona 文件 YAML 头部不完整: {path}")
                    self.persona_text = content
            else:
                self.persona_text = content
        else:
            print(f"  ⚠️ [{self.cfg.bot_name}] persona 文件不存在: {path}，bot 将无约束运行！")

    def get_event(self, chat_id: str) -> asyncio.Event:
        if chat_id not in self.events:
            self.events[chat_id] = asyncio.Event()
        return self.events[chat_id]

    def _get_cooldown_wait(self, chat_id: str) -> tuple[float, str, str]:
        """返回 (需等待秒数, 冷却标签, 详情字符串)。0=可立即回复。"""
        now = time.time()

        # ── @ 打断 LEAVE：被 @ 时取消离开，强制本轮评估 ──
        if chat_id in self.mentioned_in:
            self.mentioned_in.discard(chat_id)
            had_leave = self.leave_until.pop(chat_id, None)
            label = "提及(离开打断)" if had_leave else "提及"
            # 强制本轮评估；普通冷却不修改，靠 reply_history 自然流转
            return (0, label, "被@打断")

        # ── LEAVE 检查：暂时离开期间不回复 ──
        leave_end = self.leave_until.get(chat_id, 0)
        if now < leave_end:
            remain = leave_end - now
            return (remain, "离开", f"暂时离开，{remain:.0f}s 后恢复观察")
        if leave_end > 0:
            del self.leave_until[chat_id]

        history = self.reply_history.get(chat_id, [])
        history = [t for t in history if now - t < RATE_CAP_WINDOW]
        self.reply_history[chat_id] = history
        count_1m = sum(1 for t in history if now - t < RATE_TIER1_WINDOW)
        count_5m = sum(1 for t in history if now - t < RATE_TIER2_WINDOW)
        count_10m = len(history)
        if count_10m >= RATE_CAP_COUNT:
            oldest = history[0]
            wait = RATE_CAP_WINDOW - (now - oldest) + 1
            return (wait, "上限", f"触发上限 {count_10m}/{RATE_CAP_COUNT}条")
        skips = self.skip_history.get(chat_id, [])
        consecutive = 0
        for is_skip in reversed(skips):
            if is_skip:
                consecutive += 1
            else:
                break
        if consecutive == 0 and skips and not skips[-1]:
            cooldown = SKIP_COOLDOWN_REPLY
            skip_label = "回复"
        elif consecutive >= 5:
            cooldown = SKIP_COOLDOWN_5
            skip_label = f"{consecutive}连SKIP"
        elif consecutive >= 3:
            cooldown = SKIP_COOLDOWN_3
            skip_label = f"{consecutive}连SKIP"
        elif consecutive >= 2:
            cooldown = SKIP_COOLDOWN_2
            skip_label = f"{consecutive}连SKIP"
        elif consecutive >= 1:
            cooldown = SKIP_COOLDOWN_1
            skip_label = f"{consecutive}连SKIP"
        else:
            cooldown = SKIP_COOLDOWN_1
            skip_label = "首次"
        base = cooldown
        if count_5m >= RATE_TIER2_COUNT:
            cooldown *= RATE_TIER2_FACTOR
            rate_label = f"×{RATE_TIER2_FACTOR}"
            detail = f"{skip_label} {base}s{rate_label} ({count_5m}次/{RATE_TIER2_WINDOW}s)"
        elif count_1m >= RATE_TIER1_COUNT:
            cooldown *= RATE_TIER1_FACTOR
            rate_label = f"×{RATE_TIER1_FACTOR}"
            detail = f"{skip_label} {base}s{rate_label} ({count_1m}次/{RATE_TIER1_WINDOW}s)"
        else:
            detail = f"{skip_label} {base}s"
        since_last = now - history[-1] if history else cooldown
        if since_last < cooldown:
            return (cooldown - since_last, skip_label, detail)
        return (0, skip_label, detail)


def _load_bot_configs(cfg: dict) -> list[BotConfig]:
    """加载所有 bot 配置。

    优先从 bots.yaml，不存在或为空则回退到 config.yaml（兼容旧配置）。
    bots.yaml 模式下每个 bot 自带 ws 连接信息，无需适配器中间层。
    """
    import yaml

    shared_claude = cfg.get("claude", {})

    # ── 优先：bots.yaml ──
    bots_section: dict = {}
    from_bots_yaml = False
    if BOTS_FILE.exists():
        try:
            with open(BOTS_FILE, "r", encoding="utf-8-sig") as f:
                bots_root = yaml.safe_load(f) or {}
            bots_section = bots_root.get("bots", {})
            if bots_section:
                from_bots_yaml = True
        except Exception as e:
            print(f"⚠️ 加载 bots.yaml 失败: {e}")

    # ── 回退：config.yaml ──
    if not from_bots_yaml:
        bots_section = cfg.get("bots", {})

    # ── 最终回退：单 bot 兼容模式（ws 段直接配置）──
    if not bots_section:
        ws_cfg = cfg.get("ws", {})
        return [BotConfig(
            bot_qq="", bot_name="Anonther", persona="qq-reply",
            ws_url=ws_cfg.get("url", "ws://127.0.0.1:3001"),
            access_token=ws_cfg.get("access_token", ""),
            claude_model=shared_claude.get("model", "deepseek-flash[1M]"),
            claude_max_turns=shared_claude.get("max_turns", 10),
            claude_timeout=shared_claude.get("timeout", 120),
            api_key=shared_claude.get("api_key", ""),
            admins=[],
        )]

    # ── bots.yaml 模式：bot 自带 ws 配置，无需适配器 ──
    if from_bots_yaml:
        result = []
        for qq, bc in bots_section.items():
            qq_str = str(qq)
            ws_cfg = bc.get("ws", {})
            cl_override = bc.get("claude", {})
            result.append(BotConfig(
                bot_qq=qq_str,
                bot_name=bc.get("name", qq_str),
                persona=bc.get("persona", "qq-reply"),
                ws_url=ws_cfg.get("url", "ws://127.0.0.1:3001"),
                access_token=ws_cfg.get("access_token", ""),
                claude_model=cl_override.get("model", shared_claude.get("model", "deepseek-flash[1M]")),
                claude_max_turns=cl_override.get("max_turns", shared_claude.get("max_turns", 10)),
                claude_timeout=cl_override.get("timeout", shared_claude.get("timeout", 120)),
                api_key=shared_claude.get("api_key", ""),
                admins=bc.get("admins", []),
            ))
        print(f"🤖 从 bots.yaml 加载了 {len(result)} 个 Bot")
        return result

    # ── config.yaml 模式（旧逻辑，带适配器）──
    active_adapter = cfg.get("active_adapter", "napcat")
    adapters_cfg = cfg.get("adapters", {})
    adapter_cfg = adapters_cfg.get(active_adapter, {})
    adapter_bots = adapter_cfg.get("bots", {}) if adapter_cfg else {}
    adapter_name = adapter_cfg.get("name", active_adapter)

    result = []
    for qq, bc in bots_section.items():
        qq_str = str(qq)
        ab = adapter_bots.get(qq, {}) if isinstance(qq, int) else adapter_bots.get(int(qq), {})
        ws_cfg = ab.get("ws", bc.get("ws", {}))
        cl_override = bc.get("claude", {})
        result.append(BotConfig(
            bot_qq=qq_str,
            bot_name=bc.get("name", qq_str),
            persona=bc.get("persona", "qq-reply"),
            ws_url=ws_cfg.get("url", "ws://127.0.0.1:3001"),
            access_token=ws_cfg.get("access_token", ""),
            claude_model=cl_override.get("model", shared_claude.get("model", "deepseek-flash[1M]")),
            claude_max_turns=cl_override.get("max_turns", shared_claude.get("max_turns", 10)),
            claude_timeout=cl_override.get("timeout", shared_claude.get("timeout", 120)),
            api_key=shared_claude.get("api_key", ""),
            admins=bc.get("admins", []),
        ))
    print(f"🔌 适配器: {adapter_name}（从 config.yaml 加载）")
    return result
