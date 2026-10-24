"""
指令处理模块 —— 斜杠命令、配置重载、AI 开关
"""
from __future__ import annotations

import re
import random
import ast as _ast
import operator as _op
from datetime import datetime

import config
import storage
import cq_utils


# ==================== 安全数学求值 ====================

_SAFE_MATH_OPS = {
    _ast.Add: _op.add, _ast.Sub: _op.sub, _ast.Mult: _op.mul,
    _ast.Div: _op.truediv, _ast.Mod: _op.mod, _ast.Pow: _op.pow,
    _ast.USub: _op.neg,
}


def _safe_eval_math(expr: str) -> float | int:
    """安全的数学表达式求值，仅支持 + - * / % ** 和括号"""
    def _eval(node):
        if isinstance(node, _ast.Expression):
            return _eval(node.body)
        if isinstance(node, _ast.Constant):
            return node.value
        if isinstance(node, _ast.BinOp):
            op = _SAFE_MATH_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"不支持的操作符")
            return op(_eval(node.left), _eval(node.right))
        if isinstance(node, _ast.UnaryOp):
            op = _SAFE_MATH_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"不支持的一元操作符")
            return op(_eval(node.operand))
        raise ValueError(f"不支持的表达式")

    if not expr.strip():
        raise ValueError("空表达式")
    tree = _ast.parse(expr.strip(), mode="eval")
    return _eval(tree)


# ==================== 发送辅助 ====================

async def _cmd_reply(ws, event: dict, msg: str):
    """命令回复：根据消息类型自动选择群聊或私聊发送。@了特定 bot 则用该 bot 身份回复。"""
    targeted = event.get("_targeted") or []
    if targeted and targeted[0].ws:
        ws = targeted[0].ws
    if event.get("message_type", "group") == "group":
        await cq_utils.send_group_msg(ws, event.get("group_id"), msg)
    else:
        await cq_utils.send_private_msg(ws, event.get("user_id"), msg)


def _check_admin(ws, event: dict) -> bool:
    """检查发送者是否在 bot 的管理员列表中。admins 为空则不限制。"""
    bot = config._find_bot_by_ws(ws)
    admins = bot.cfg.admins if bot else []
    if not admins:
        return True
    return str(event.get("user_id", "")) in [str(a) for a in admins]


# ==================== 基础指令 ====================

async def handle_help(ws, event: dict):
    text = """📋 可用指令：
/help          - 显示此帮助
/ping          - 测试延迟
/time          - 服务器时间
/echo xxx      - 复读
/roll          - 掷骰子 (1-100)
/calc 表达式   - 简单计算
/clear [数字]  - 清除对话记忆（可只清最近 N 条）
/reload        - 热重载配置
/ai on|off|status [--all] - 开关/查看 AI 回复

💬 直接 @我 或私聊说话，我会用 AI 回复！"""
    await _cmd_reply(ws, event, text)


async def handle_ping(ws, event: dict):
    await _cmd_reply(ws, event, "🏓 pong!")


async def handle_time(ws, event: dict):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await _cmd_reply(ws, event, f"🕐 {now}")


async def handle_echo(ws, event: dict, args: str):
    if not args:
        await _cmd_reply(ws, event, "用法: /echo 要说的话")
    else:
        await _cmd_reply(ws, event, args)


async def handle_roll(ws, event: dict):
    result = random.randint(1, 100)
    await _cmd_reply(ws, event, f"🎲 你掷出了: {result}")


async def handle_calc(ws, event: dict, args: str):
    if not args:
        await _cmd_reply(ws, event, "用法: /calc 表达式 (如 /calc 1+2*3)")
        return
    try:
        safe = re.sub(r'[^0-9+\-*/().%\s]', '', args)
        result = _safe_eval_math(safe)
        await _cmd_reply(ws, event, f"🧮 {args} = {result}")
    except Exception:
        await _cmd_reply(ws, event, "❌ 表达式有误，试试 /calc 1+2*3")


async def handle_clear(ws, event: dict, args: str = ""):
    if not _check_admin(ws, event):
        await _cmd_reply(ws, event, "❌ 权限不足")
        return

    chat_id = config._chat_key(event)
    n = 0
    if args:
        try:
            n = int(args.strip())
        except ValueError:
            await _cmd_reply(ws, event, "用法: /clear 或 /clear 数字")
            return

    if n > 0:
        log = storage.load_chat_log(chat_id)
        removed = min(n, len(log))
        log = log[:-n] if n < len(log) else []
        storage.save_chat_log(chat_id, log)
        if not log:
            storage._session_cursor.pop(chat_id, None)
            storage._save_cursors()
        if log:
            last = log[-1]
            preview = last.get("message", "")[:60]
            name = last.get("nickname", "?")
            ts = last.get("time", "")
            await _cmd_reply(ws, event, f"🧹 清除了最近 {removed} 条（剩余 {len(log)} 条）\n最后一条: [{ts}] {name}: {preview}")
        else:
            await _cmd_reply(ws, event, f"🧹 清除了最近 {removed} 条（已清空）")
    else:
        storage.save_chat_log(chat_id, [])
        storage._session_cursor.pop(chat_id, None)
        storage._save_cursors()
        await _cmd_reply(ws, event, "🧹 对话记忆已清除！")


# ==================== 重载配置 ====================

async def handle_reload(ws, event: dict):
    if not _check_admin(ws, event):
        await _cmd_reply(ws, event, "❌ 权限不足")
        return

    targets = event.get("_targeted") or config._ALL_BOTS
    names = ", ".join(b.cfg.bot_name for b in targets)
    try:
        cfg_raw = config._load_config()
        config._apply_config(cfg_raw)
        # 重载 per-bot 配置（model, timeout, admins 等）——仅针对被 @ 的 bot
        new_bot_cfgs = config._load_bot_configs(cfg_raw)
        cfg_by_qq = {bc.bot_qq: bc for bc in new_bot_cfgs}
        for b in targets:
            if b.cfg.bot_qq in cfg_by_qq:
                b.cfg = cfg_by_qq[b.cfg.bot_qq]
            b._load_persona()
    except Exception as e:
        await _cmd_reply(ws, event, f"❌ 重载配置失败: {e}")
        return

    lines = [
        f"✅ 配置已重载 [{names}]：",
        f"  Bots: {len(config._ALL_BOTS)} 个",
        f"  聚合窗口: {config.AGGREGATION_WINDOW_TIMEOUT}s / {config.AGGREGATION_MAX_MESSAGES}条",
        f"  SKIP冷却: 回复{config.SKIP_COOLDOWN_REPLY}s / 1skip{config.SKIP_COOLDOWN_1}s / 2skip{config.SKIP_COOLDOWN_2}s / 3+skip{config.SKIP_COOLDOWN_3}s | LEAVE {config.LEAVE_COOLDOWN}s",
        f"  Session: soft{config.SESSION_SOFT_LIMIT} hard{config.SESSION_HARD_LIMIT} compact→{config.SESSION_COMPACT_TO} overlap{config.SESSION_OVERLAP}",
        f"  长期记忆: 最多{config.MEMORY_MAX_ENTRIES}条 | 最小compact {config.MEMORY_MIN_COMPACT}条",
        f"  频率因数: {config.RATE_TIER1_COUNT}条/{config.RATE_TIER1_WINDOW}s→×{config.RATE_TIER1_FACTOR} | "
        f"{config.RATE_TIER2_COUNT}条/{config.RATE_TIER2_WINDOW}s→×{config.RATE_TIER2_FACTOR} | "
        f"{config.RATE_CAP_COUNT}条/{config.RATE_CAP_WINDOW}s封顶",
        f"  历史: {config.MAX_HISTORY}条 / 截断{config.MAX_CONTEXT_CHARS}字",
        f"  回复上限: {config.MAX_REPLY_LENGTH}字",
        f"  群成员 TTL: {config._GROUP_MEMBER_TTL}s",
    ]
    await _cmd_reply(ws, event, "\n".join(lines))


# ==================== AI 开关 ====================

async def handle_ai(ws, event: dict, args: str):
    parts = args.strip().split()
    action = parts[0].lower() if parts else ""
    is_all = any(p in ("--all", "-a") for p in parts)

    if is_all and not _check_admin(ws, event):
        await _cmd_reply(ws, event, "❌ 权限不足（--all 需要管理员权限）")
        return

    chat_id = config._chat_key(event)
    targeted = event.get("_targeted") or []
    fallback = config._find_bot_by_ws(ws)
    if is_all:
        bots = config._ALL_BOTS
    elif targeted:
        bots = targeted
    elif fallback:
        bots = [fallback]
    else:
        bots = []

    if not bots:
        await _cmd_reply(ws, event, "❌ 没找到目标 bot")
        return

    names = ", ".join(b.cfg.bot_name for b in bots)

    if action in ("on", "1", "true", "开启", "开"):
        for b in bots:
            if is_all:
                b.chat_ai_disabled.clear()
                b.ai_enabled = True
                b.ai_override = True
            else:
                b.chat_ai_disabled.discard(chat_id)
        await _cmd_reply(ws, event, f"✅ AI 回复已开启 [{names}]" + ("（全局）" if is_all else ""))

    elif action in ("off", "0", "false", "关闭", "关"):
        for b in bots:
            if is_all:
                b.chat_ai_disabled.clear()
                b.ai_enabled = False
                b.ai_override = False
            else:
                b.chat_ai_disabled.add(chat_id)
        await _cmd_reply(ws, event, f"🛑 AI 回复已关闭 [{names}]" + ("（全局）" if is_all else ""))

    elif action == "status":
        lines = []
        for b in bots:
            if is_all:
                status = "开启" if b.ai_enabled else "关闭"
            else:
                status = "关闭" if chat_id in b.chat_ai_disabled else "开启"
            lines.append(f"  {b.cfg.bot_name}: {status}")
        await _cmd_reply(ws, event, f"📊 AI 状态 [{names}]：\n" + "\n".join(lines))

    else:
        await _cmd_reply(ws, event, "用法: /ai on|off|status [--all]")


# ==================== 命令分发表 ====================

COMMANDS = {
    "/help": handle_help,
    "/ping": handle_ping,
    "/time": handle_time,
    "/echo": handle_echo,
    "/roll": handle_roll,
    "/calc": handle_calc,
    "/clear": handle_clear,
    "/reload": handle_reload,
    "/ai": handle_ai,
}
