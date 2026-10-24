"""
OneBot QQ 机器人 — 基于 OneBot11 WebSocket 协议
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import shutil
from datetime import datetime
from pathlib import Path

import websockets

import config
import storage
import cq_utils
import media_store
import vision
import commands


# ==================== 消息去重 ====================
_seen_messages: dict[str, set[int]] = {}  # chat_id → {message_id, ...}
MAX_SEEN_PER_CHAT = 500

# bilibili-mcp 只保留搜索工具，其余工具全部从模型工具列表中移除
_BILI_DENY_TOOLS = [
    "bili_login", "bili_login_check", "bili_check_credential",
    "bili_danmaku", "bili_reply",
    "bili_send_dynamic", "bili_upload_video", "bili_upload_video_multi",
    "bili_send_opus", "bili_video_zones", "bili_hot_videos", "bili_hot_buzzwords",
    "bili_weekly_hot", "bili_rank", "bili_user_info", "bili_user_videos",
    "bili_favorite_lists", "bili_favorite_content", "bili_send_message",
    "bili_unread_messages", "bili_received_replies", "bili_received_at_and_likes",
]
_BILI_DENY = ",".join(f"mcp__bilibili-mcp__{n}" for n in _BILI_DENY_TOOLS)


# ==================== Bot 监控 ====================

async def _chat_monitor(bot: "config.BotInstance", chat_id: str):
    """独立 per-bot 监控：聚合 → 冷却 → AI 回复 → 循环。"""
    evt = bot.get_event(chat_id)
    idle_timeout = config.REPLY_COOLDOWN + 30

    try:
        while True:
            # Phase 0: 等第一条新消息
            while bot.chat_has_new.get(chat_id, 0) == 0:
                evt.clear()
                try:
                    await asyncio.wait_for(evt.wait(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    return

            # Phase 0.5: 聚合窗口
            msg_count = bot.chat_has_new.get(chat_id, 0)
            while msg_count < config.AGGREGATION_MAX_MESSAGES:
                evt.clear()
                try:
                    await asyncio.wait_for(evt.wait(), timeout=config.AGGREGATION_WINDOW_TIMEOUT)
                    msg_count = bot.chat_has_new.get(chat_id, 0)
                except asyncio.TimeoutError:
                    break

            bot.chat_has_new.pop(chat_id, 0)

            # Phase 1: 冷却
            wait_sec, label, detail = bot._get_cooldown_wait(chat_id)
            if wait_sec > 0:
                capped_wait = min(wait_sec, 30.0)
                print(f"  ⏳ [{bot.cfg.bot_name}] 冷却 [{detail}] 还需{wait_sec:.0f}s")
                evt.clear()
                try:
                    await asyncio.wait_for(evt.wait(), timeout=capped_wait)
                except asyncio.TimeoutError:
                    if capped_wait < wait_sec:
                        continue
                else:
                    continue

            # Phase 2: 调 AI
            if not bot.ws:
                print(f"  ⚠️ [{bot.cfg.bot_name}] 未连接，跳过回复 [chat={chat_id}]")
            elif bot.ai_enabled and chat_id not in bot.chat_ai_disabled and not bot.heavy_task_active:
                parts = chat_id.split("-")
                msg_type = parts[0]
                target_id = parts[-1]
                # 私聊：self_id 需匹配当前 bot，防止 bot B 用 bot A 的私聊日志生成回复
                if msg_type == "private":
                    private_self_id = parts[1] if len(parts) >= 2 else ""
                    if private_self_id != str(bot.cfg.bot_qq):
                        continue
                event = {
                    "message_type": msg_type,
                    "group_id": int(target_id) if msg_type == "group" else None,
                    "user_id": int(target_id) if msg_type == "private" else None,
                    "self_id": int(bot.cfg.bot_qq),
                    "sender": {},
                    "time": 0,
                }
                await ai_chat(bot, event, "")
                bot.reply_history.setdefault(chat_id, []).append(time.time())
            else:
                print(f"  ⏸️ [{bot.cfg.bot_name}] AI 已关闭，跳过回复 [chat={chat_id}]")
    finally:
        bot.chat_busy.discard(chat_id)


# ==================== 消息分发 ====================

async def _dispatch_message(event: dict):
    """所有消息的统一入口：去重 → seq → handle_message → 触发各 bot monitor"""
    chat_id = config._chat_key(event)
    msg_id = event.get("message_id")

    # === 去重 ===
    if msg_id:
        if chat_id not in _seen_messages:
            _seen_messages[chat_id] = set()
        if msg_id in _seen_messages[chat_id]:
            return
        _seen_messages[chat_id].add(msg_id)
        if len(_seen_messages[chat_id]) > MAX_SEEN_PER_CHAT:
            _seen_messages[chat_id] = set(list(_seen_messages[chat_id])[-MAX_SEEN_PER_CHAT:])

    # === bot 自己发的消息：只去重不处理 ===
    sender_qq = str(event.get("user_id", ""))
    if sender_qq in config._BOT_QQS:
        return

    # === 黑名单群 ===
    if event.get("message_type") == "group":
        group_id = str(event.get("group_id", ""))
        if group_id in config.BLACKLIST_GROUPS:
            return

    # === seq 分配 ===
    seq = await storage._request_seq(chat_id)
    event["_seq"] = seq

    # === 统一处理 ===
    handler_bot = None
    for b in config._ALL_BOTS:
        if b.ws:
            handler_bot = b
            break
    if handler_bot:
        await _safe_handle_message(handler_bot.ws, event)
    else:
        text = ""
        raw = event.get("raw_message", event.get("message", ""))
        if isinstance(raw, list):
            text = "".join(seg.get("data", {}).get("text", "") if seg.get("type") == "text" else f"[{seg.get('type')}]" for seg in raw)
        else:
            text = str(raw)
        if text:
            sender = event.get("sender", {})
            name = sender.get("card") or sender.get("nickname") or str(event.get("user_id", "?"))
            entry = {
                "time": datetime.fromtimestamp(event.get("time", 0)).strftime("%m-%d %H:%M:%S"),
                "user_id": event.get("user_id"),
                "nickname": name,
                "message": text,
                "message_id": event.get("message_id"),
            }
            await _ordered_write(chat_id, seq, entry, trigger=True)
        else:
            await _fill_missing_seq(chat_id, seq)


# ==================== WebSocket 连接 ====================

async def bot_connect(bot: "config.BotInstance"):
    """单个 bot 的 WebSocket 连接 + 重连循环"""
    url = bot.cfg.ws_url
    if bot.cfg.access_token:
        url += f"?access_token={bot.cfg.access_token}"

    retry_delay = 1
    max_delay = 30
    was_connected = False
    printed_connecting = False

    while True:
        try:
            if not printed_connecting:
                print(f"[{bot.cfg.bot_name}] 正在连接 {bot.cfg.ws_url} ...")
                printed_connecting = True
            async with websockets.connect(url, max_size=10*1024*1024,
                                          ping_interval=30, ping_timeout=10) as ws:
                bot.ws = ws
                print(f"[{bot.cfg.bot_name}] ✅ 已连接")
                retry_delay = 1
                was_connected = True

                async for raw_msg in ws:
                    if isinstance(raw_msg, bytes):
                        raw_msg = raw_msg.decode("utf-8")
                    event = json.loads(raw_msg)

                    post_type = event.get("post_type", "")
                    if post_type == "message":
                        asyncio.create_task(_dispatch_message(event))
                    elif "echo" in event:
                        storage._handle_api_response(event)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[{bot.cfg.bot_name}] ⚠️ 断开: {e}")
            was_connected = False
        except (ConnectionRefusedError, OSError) as e:
            was_connected = False
        except asyncio.TimeoutError:
            print(f"[{bot.cfg.bot_name}] ⚠️ 连接超时")
            was_connected = False
        except Exception as e:
            print(f"[{bot.cfg.bot_name}] ❌ 异常: {e}")
            was_connected = False
        else:
            print(f"[{bot.cfg.bot_name}] ⚠️ 服务器关闭连接")
            was_connected = False

        bot.ws = None
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, max_delay)


# ==================== 有序写入 & 日志 ====================

def _is_log_saturated(chat_id: str) -> bool:
    """日志已满且 cursor=0 → 暂停写盘，强制 compact"""
    log = storage.load_chat_log(chat_id)
    if len(log) < config.LOG_MAX_ENTRIES:
        return False
    storage._ensure_cursors_loaded()
    if storage._session_cursor.get(chat_id, 0) != 0:
        return False

    # 硬间隔检查在 _ordered_write 里触发，这里只阻塞
    return True


async def _ordered_write(chat_id: str, seq: int, entry: dict, trigger: bool = False,
                        sender_bot_qq: str = ""):
    """按消息到达顺序写入日志条目。trigger=True 表示写盘后触发 AI 回复。"""

    # 日志已满且 cursor=0：丢弃内容但必须占用 seq，避免后续消息等缺口；
    # 同时确保 compact 被触发。
    if _is_log_saturated(chat_id):
        storage._check_hard_limit(chat_id)
        entry = {"_cmd": True}
        trigger = False

    trigger_count = 0
    async with storage._write_flush_lock:
        if chat_id not in storage._pending_writes:
            storage._pending_writes[chat_id] = {}
        slot = storage._pending_writes[chat_id]
        next_seq = storage._next_write_seq.get(chat_id, 1)

        if seq < next_seq:
            if not entry.get("_cmd"):
                log = storage.load_chat_log(chat_id)
                log.append(entry)
                storage.save_chat_log(chat_id, log)
                storage._check_hard_limit(chat_id)
                if trigger:
                    trigger_count = 1
        else:
            slot[seq] = (entry, time.time())

            if next_seq not in slot and slot:
                oldest_ts = min(ts for _, ts in slot.values())
                if time.time() - oldest_ts > 5:
                    jumped_from = next_seq
                    next_seq = min(slot.keys())
                    print(f"  ⚠️ seq 缺口超时 [{chat_id}]: {jumped_from}→{next_seq}")
                    # 被跳过的 seq 永远不会写盘，清掉它们的待触发标记
                    if chat_id in storage._pending_triggers:
                        for s in range(jumped_from, next_seq):
                            storage._pending_triggers[chat_id].discard(s)

            log = storage.load_chat_log(chat_id)
            flushed_seqs = []
            while next_seq in slot:
                e, _ts = slot.pop(next_seq)
                if not e.get("_cmd"):
                    log.append(e)
                flushed_seqs.append(next_seq)
                next_seq += 1
            if flushed_seqs:
                storage.save_chat_log(chat_id, log)
                storage._check_hard_limit(chat_id)
            storage._next_write_seq[chat_id] = next_seq

            if trigger and seq in flushed_seqs:
                trigger_count += 1
            if chat_id in storage._pending_triggers:
                for s in flushed_seqs:
                    if s in storage._pending_triggers[chat_id]:
                        storage._pending_triggers[chat_id].discard(s)
                        trigger_count += 1
                if not storage._pending_triggers[chat_id]:
                    del storage._pending_triggers[chat_id]

            if trigger and seq not in flushed_seqs:
                if chat_id not in storage._pending_triggers:
                    storage._pending_triggers[chat_id] = set()
                storage._pending_triggers[chat_id].add(seq)

    if trigger_count > 0:
        for bot in config._ALL_BOTS:
            if sender_bot_qq and bot.cfg.bot_qq == sender_bot_qq:
                continue
            if not bot.ai_enabled:
                continue
            # 群聊：检查 bot 是否在群成员中（不在群里则不触发）
            if chat_id.startswith("group-"):
                group_id = int(chat_id.split("-", 1)[1])
                members = storage._get_group_members(group_id)
                if members and bot.cfg.bot_qq not in members:
                    continue
            bot.chat_has_new[chat_id] = bot.chat_has_new.get(chat_id, 0) + trigger_count
            bot.get_event(chat_id).set()
            async with bot.spawn_lock:
                if chat_id not in bot.chat_busy:
                    bot.chat_busy.add(chat_id)
                    print(f"  🤖 启动监测 [{bot.cfg.bot_name}] [chat={chat_id}]")
                    asyncio.create_task(_chat_monitor(bot, chat_id))


async def _fill_missing_seq(chat_id: str, seq: int):
    """确保已分配的 seq 被占用，避免后续消息等待缺口。"""
    if seq <= 0:
        return
    if seq < storage._next_write_seq.get(chat_id, 1):
        return  # 已经写入
    if seq in storage._pending_writes.get(chat_id, {}):
        return  # 真实消息已经排队
    await _ordered_write(chat_id, seq, {"_cmd": True}, trigger=False)


async def log_message(event: dict, text: str, trigger: bool = True, images: list | None = None):
    """记录一条用户消息（有序写入，写盘后可触发 AI 回复）"""
    chat_id = config._chat_key(event)
    seq = event.get("_seq", 0)
    sender = event.get("sender", {})
    entry = {
        "time": datetime.fromtimestamp(event.get("time", 0)).strftime("%m-%d %H:%M:%S"),
        "user_id": event.get("user_id"),
        "nickname": sender.get("card") or sender.get("nickname") or str(event.get("user_id")),
        "message": text,
        "message_id": event.get("message_id"),
    }
    if images:
        entry["images"] = images
    await _ordered_write(chat_id, seq, entry, trigger=trigger)


async def log_bot_reply(bot: "config.BotInstance", event: dict, text: str, seq: int, message_id=None):
    """记录机器人回复，写盘后触发其他 bot（跳过自己）"""
    chat_id = config._chat_key(event)
    entry = {
        "time": datetime.now().strftime("%m-%d %H:%M:%S"),
        "user_id": bot.cfg.bot_qq,
        "nickname": bot.cfg.bot_name,
        "bot": True,
        "message": text.strip(),
    }
    if message_id:
        entry["message_id"] = message_id
        if chat_id not in _seen_messages:
            _seen_messages[chat_id] = set()
        _seen_messages[chat_id].add(message_id)
    await _ordered_write(chat_id, seq, entry, trigger=True, sender_bot_qq=bot.cfg.bot_qq)


# ==================== 回复发送 ====================

async def reply(ws, event: dict, message: str, *, log: bool = True):
    """根据消息来源自动回复到群或私聊"""
    log_text = message
    bot = config._find_bot_by_ws(ws)
    bot_name = bot.cfg.bot_name if bot else ""
    message = cq_utils._resolve_stickers(message, bot_name)
    if not message.strip():
        return
    if event.get("message_type") == "group":
        message = cq_utils._resolve_at_mentions(message, event)
    if len(message) > config.MAX_REPLY_LENGTH:
        message = message[:config.MAX_REPLY_LENGTH - 12] + "…[已截断]"

    msg_type = event.get("message_type", "group")
    if msg_type == "group":
        resp = await storage._api_call(ws, "send_group_msg", {"group_id": event["group_id"], "message": message})
    else:
        resp = await storage._api_call(ws, "send_private_msg", {"user_id": event["user_id"], "message": message})

    message_id = resp.get("data", {}).get("message_id") if isinstance(resp.get("data"), dict) else None

    if log:
        chat_id = config._chat_key(event)
        bot = config._find_bot_by_ws(ws)
        if bot:
            seq = await storage._request_seq(chat_id)
            try:
                await log_bot_reply(bot, event, log_text, seq=seq, message_id=message_id)
            except Exception as e:
                print(f"  ❌ 记录 bot 回复失败 [{chat_id}]: {e}")
                await _fill_missing_seq(chat_id, seq)


# ==================== Claude Code CLI ====================

async def call_claude_for_reply(bot: "config.BotInstance", content: str | list[dict], max_turns: int | None = None, timeout: int | None = None) -> tuple[str | None, float]:
    """调用 Claude Code CLI 获取回复。content 可以是纯文本或 content blocks。返回 (reply_text, elapsed_seconds)。"""
    if max_turns is None:
        max_turns = bot.cfg.claude_max_turns
    if timeout is None:
        timeout = bot.cfg.claude_timeout

    if isinstance(content, str):
        content_blocks = [{"type": "text", "text": content}]
    else:
        content_blocks = content

    api_key = bot.cfg.api_key or config.CLAUDE_API_KEY

    cmd = [
        config._claude_exe,
        "-p",
        "--input-format", "stream-json",
        "--model", bot.cfg.claude_model,
        "--max-turns", str(max_turns),
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--resume", bot.session_id,
        "--setting-sources", "",
        "--tools", "WebSearch,WebFetch",
        "--disable-slash-commands",
    ]

    # 只加载显式配置的 MCP，并把 bilibili-mcp 收敛到 bili_search
    mcp_config = config.BASE_DIR / ".mcp.json"
    if mcp_config.exists():
        cmd += [
            "--strict-mcp-config",
            "--mcp-config", str(mcp_config),
            "--disallowedTools", _BILI_DENY,
        ]

    # --setting-sources 为空后，settings.json 不再生效；用 --settings 显式传 base_url/key
    settings_override = config.claude_settings_json(api_key)
    if settings_override:
        cmd += ["--settings", settings_override]

    async with bot.claude_lock:
        # 全新机器上 projects 目录可能还不存在，先创建
        bot.session_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(bot.empty_session), str(bot.session_file))

        t0 = time.time()
        try:
            subprocess_env = os.environ.copy()
            subprocess_env.pop("ENABLE_TOOL_SEARCH", None)
            subprocess_env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"
            subprocess_env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"
            subprocess_env["CLAUDE_CODE_DISABLE_PLUGINS"] = "1"
            subprocess_env["CLAUDE_CODE_DISABLE_SKILLS"] = "1"

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(config.BASE_DIR),
                env=subprocess_env,
            )

            # stream-json 输入：写入一条 user message（文字/图片 content blocks），然后关闭 stdin
            payload = json.dumps(
                {"type": "user", "message": {"role": "user", "content": content_blocks}},
                ensure_ascii=False,
            ) + "\n"
            try:
                proc.stdin.write(payload.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception as e:
                print(f"⚠️ 写入 CLI stdin 失败: {e}")

            async def _stream_stdout(p):
                while True:
                    line = await p.stdout.readline()
                    if not line:
                        break
                    yield line.decode("utf-8", errors="replace")

            async def _read_stderr():
                chunks = []
                async for chunk in proc.stderr:
                    chunks.append(chunk)
                return b"".join(chunks)

            stderr_task = asyncio.create_task(_read_stderr())

            reply_text = None
            usage = {}
            turns = 0
            cost = 0
            tool_count = 0
            is_error = False
            error_type = ""

            timed_out = False
            try:
                async with asyncio.timeout(float(timeout)):
                    async for line_raw in _stream_stdout(proc):
                        line = line_raw.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type", "")

                        if etype == "assistant":
                            msg = event.get("message", {})
                            for block in (msg.get("content") or []):
                                if not isinstance(block, dict):
                                    continue
                                bt = block.get("type", "")
                                if bt in ("tool_use", "tool_call"):
                                    tool = block.get("name", "?").replace("mcp__qq-rag__", "").replace("mcp__bilibili-mcp__", "")
                                    inp = block.get("input", {})
                                    args = ", ".join(f"{k}={v}" for k, v in inp.items())
                                    print(f"  🔧 {tool}({args})")
                                    tool_count += 1
                                elif bt == "thinking":
                                    raw = block.get("thinking", "")
                                    if len(raw) > 500:
                                        thought = raw[:200].replace("\n", " ") + " … " + raw[-200:].replace("\n", " ")
                                    else:
                                        thought = raw.replace("\n", " ")
                                    if thought:
                                        print(f"  🧠 {thought}")

                        elif etype == "result":
                            result_text = event.get("result", "")
                            if result_text:
                                reply_text = result_text.strip()
                            turns = event.get("num_turns", "?")
                            cost = event.get("total_cost_usd", 0)
                            usage = event.get("usage", {})
                            is_error = event.get("is_error", False)
                            error_type = event.get("subtype", "")
            except TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                stderr_task.cancel()

            elapsed = time.time() - t0

            if timed_out:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                print(f"⚠️ Claude CLI 超时 ({timeout}s)")
                return None, elapsed

            try:
                stderr_data = await asyncio.wait_for(stderr_task, timeout=5)
            except asyncio.TimeoutError:
                stderr_data = b""
                stderr_task.cancel()
            stderr_err = stderr_data.decode("utf-8", errors="replace").strip()

            await proc.wait()

            if is_error and not reply_text:
                print(f"⚠️ Claude 返回错误: {error_type} (exit={proc.returncode})")
                if stderr_err:
                    print(f"📛 stderr: {stderr_err[:300]}")
                return None, elapsed

            if proc.returncode != 0:
                print(f"⚠️ Claude CLI exit={proc.returncode} error={error_type} "
                      f"reply={'found' if reply_text else 'none'} "
                      f"stderr={'yes' if stderr_err else 'no'}")

            if reply_text is None:
                print(f"⚠️ stream-json 未找到有效回复")
                return None, elapsed

            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cache_hit = usage.get("cache_read_input_tokens") or usage.get("prompt_cache_hit_tokens", 0)
            total_context = input_tokens + cache_hit
            hit_rate = cache_hit / total_context * 100 if total_context > 0 else 0

            print(f"⏱️ {elapsed:.1f}s | {turns} turns ${cost:.4f} | "
                  f"in:{input_tokens}(new) out:{output_tokens} 🔧={tool_count}")
            print(f"💾 cache: hit={cache_hit} total={total_context} rate={hit_rate:.0f}%")

            return reply_text, elapsed

        except FileNotFoundError:
            elapsed = time.time() - t0
            print(f"❌ claude CLI 未找到，请确认已安装 Claude Code")
            return None, elapsed
        except Exception as e:
            elapsed = time.time() - t0
            print(f"❌ Claude CLI 调用失败: {e}")
            return None, elapsed


# ==================== AI 聊天 ====================

async def ai_chat(bot: "config.BotInstance", event: dict, user_text: str):
    """通过 Claude Code CLI (qq-reply skill) 获取回复。"""
    chat_id = config._chat_key(event)
    history_blocks, history_text = await get_chat_context_blocks(event)
    print("─" * 40)
    print("🤖 Claude Code ✅ 开始回复")
    ts = datetime.fromtimestamp(event.get("time", 0)).strftime("%H:%M:%S")
    sender = event.get("sender", {})
    name = sender.get("card") or sender.get("nickname") or str(event.get("user_id", "?"))

    now_str = datetime.now().strftime("%Y年%m月%d日 %A %H:%M:%S")
    for en, zh in [("Monday","周一"),("Tuesday","周二"),("Wednesday","周三"),
                   ("Thursday","周四"),("Friday","周五"),("Saturday","周六"),("Sunday","周日")]:
        now_str = now_str.replace(en, zh)

    content_blocks: list[dict] = []
    header_parts: list[str] = []
    if bot.persona_text:
        header_parts.append(bot.persona_text)
    memory_text = storage.format_memory_for_prompt(chat_id)
    if memory_text:
        header_parts.append(memory_text)
    if header_parts:
        content_blocks.append({"type": "text", "text": "\n\n".join(header_parts)})

    if any(b.get("type") == "image" for b in history_blocks):
        content_blocks.append({
            "type": "text",
            "text": "【图片说明】图片按聊天中的位置理解；多帧表情属于同一个动图。不要统计或提及图片数量。",
        })

    if history_blocks:
        sep = "\n\n" if header_parts else ""
        content_blocks.append({"type": "text", "text": f"{sep}【聊天记录】\n"})
        content_blocks.extend(history_blocks)
    else:
        sep = "\n\n" if header_parts else ""
        content_blocks.append({"type": "text", "text": f"{sep}【当前消息】\n{user_text}"})
    content_blocks.append({"type": "text", "text": f"\n\n当前时间：{now_str}"})

    if history_text:
        lines = history_text.split("\n")
        tail = "\n".join(lines[-4:])
        print(f"  📝 聊天记录共{len(lines)}行，最后4行:\n  {tail.replace(chr(10), chr(10)+'  ')}")
    else:
        print(f"  📝 无聊天记录，当前消息: {user_text[:80]}")

    reply_text, elapsed = await call_claude_for_reply(bot, content_blocks)
    if reply_text is None:
        bot.skip_history.setdefault(chat_id, []).append(True)
        print("─" * 40)
        return

    # [LEAVE] 检查
    if reply_text.strip() == "[LEAVE]":
        bot.leave_until[chat_id] = time.time() + config.LEAVE_COOLDOWN
        bot.skip_history.setdefault(chat_id, []).append(True)
        print(f"  🚪 [{bot.cfg.bot_name}] 暂时离开 [{chat_id}] {config.LEAVE_COOLDOWN}s")
        print("─" * 40)
        return

    # [SKIP] 检查
    if reply_text.strip() == "[SKIP]" or reply_text.strip() == "":
        bot.skip_history.setdefault(chat_id, []).append(True)
        print(f"  ⏭️ [{bot.cfg.bot_name}] [SKIP] [{chat_id}]")
        print("─" * 40)
        return

    if not reply_text.strip():
        bot.skip_history.setdefault(chat_id, []).append(True)
        print("─" * 40)
        return

    bot.skip_history.setdefault(chat_id, []).append(False)

    # 分段
    segments = cq_utils._split_long_text(reply_text, config.MAX_REPLY_LENGTH)
    segments = cq_utils._merge_overflow_segments(segments, config.MAX_SEGMENTS)
    # 切分后多余的 [SKIP]/[LEAVE] 标记过滤，不发出不写盘
    segments = [s for s in segments if s.strip().upper() not in ("[SKIP]", "[LEAVE]")]

    print(f"📤 [{bot.cfg.bot_name}] 分段回复 ({len(segments)}段)")
    for i, seg in enumerate(segments):
        print(f"  [{i+1}/{len(segments)}] {seg[:100]}{'...' if len(seg)>100 else ''}")

    # 发送
    _is_sticker = re.compile(r'^\[(?:动画表情|sticker|贴纸):[^\]]+\]$')
    for i, seg in enumerate(segments):
        if i > 0:
            delay = config.REPLY_SEGMENT_DELAY_BASE
            if not _is_sticker.match(seg) and len(seg) > config.REPLY_SEGMENT_DELAY_CHAR_THRESHOLD:
                delay += config.REPLY_SEGMENT_DELAY_PER_CHAR * (len(seg) - config.REPLY_SEGMENT_DELAY_CHAR_THRESHOLD)
            await asyncio.sleep(delay)
        await reply(bot.ws, event, seg, log=True)
    print("─" * 40)


# ==================== 消息处理 ====================

async def get_chat_context(event: dict) -> str:
    """获取当前聊天上下文（文本格式）。"""
    chat_id = config._chat_key(event)
    log = storage.load_chat_log(chat_id)

    qq_to_name: dict[str, str] = {}
    for entry in log:
        uid = entry.get("user_id")
        name = entry.get("nickname", "")
        if uid and name:
            qq_to_name[str(uid)] = name

    storage._check_hard_limit(chat_id)
    storage._ensure_cursors_loaded()

    start = storage._session_cursor.get(chat_id, 0)
    session_len = len(log) - start

    lines = []
    for entry in log[start:]:
        if entry.get("_cmd"):
            continue
        ts = entry.get("time", "")
        segments = cq_utils._cq_string_to_segments(str(entry.get("message", "")))
        msg = await cq_utils._msg_segments_to_text(segments, qq_to_name)
        if len(msg) > config.MAX_CONTEXT_CHARS:
            msg = msg[:config.MAX_CONTEXT_CHARS] + "…"
        if entry.get("bot"):
            name = entry.get("nickname", "Bot")
            lines.append(f"[{ts}] {name}: {msg}")
        else:
            name = entry.get("nickname", "?")
            lines.append(f"[{ts}] {name}: {msg}")
    print(f"  📊 context: JSON {len(log)}条 cursor={start} session={session_len} → {len(lines)}行输出")
    return "\n".join(lines)


async def get_chat_context_blocks(event: dict) -> tuple[list[dict], str]:
    """获取当前聊天上下文，返回 (content blocks, 纯文本用于日志)。

    图片按记录中的占位符顺序交错插入，字节直接复用落盘文件 → 前缀缓存稳定。
    """
    chat_id = config._chat_key(event)
    log = storage.load_chat_log(chat_id)

    qq_to_name: dict[str, str] = {}
    for entry in log:
        uid = entry.get("user_id")
        name = entry.get("nickname", "")
        if uid and name:
            qq_to_name[str(uid)] = name

    storage._check_hard_limit(chat_id)
    storage._ensure_cursors_loaded()

    start = storage._session_cursor.get(chat_id, 0)
    session_len = len(log) - start

    blocks: list[dict] = []
    lines: list[str] = []
    image_count = 0
    first = True
    prev_user: str | None = None
    prev_dt = None
    prev_is_user = False

    for entry in log[start:]:
        if entry.get("_cmd"):
            continue
        ts = entry.get("time", "")
        segments = cq_utils._cq_string_to_segments(str(entry.get("message", "")))
        msg = await cq_utils._msg_segments_to_text(segments, qq_to_name)
        if len(msg) > config.MAX_CONTEXT_CHARS:
            msg = msg[:config.MAX_CONTEXT_CHARS] + "…"
        name = entry.get("nickname", "Bot" if entry.get("bot") else "?")
        is_bot = bool(entry.get("bot"))
        uid = str(entry.get("user_id") or name)
        dt = media_store.parse_log_time(ts)
        can_merge = (
            not is_bot
            and prev_is_user
            and uid == prev_user
            and dt is not None
            and prev_dt is not None
            and 0 <= (dt - prev_dt).total_seconds() <= media_store.MERGE_WINDOW_SECONDS
        )

        full_prefix = f"[{ts}] {name}: "
        if can_merge:
            display_prefix = media_store.CONTINUATION_PREFIX
            block_prefix = "\n" + display_prefix
        else:
            display_prefix = full_prefix
            block_prefix = ("" if first else "\n") + display_prefix
        first = False
        lines.append(display_prefix + msg)

        entry_blocks = media_store.build_blocks(
            msg, entry.get("images") or [], prefix=block_prefix
        )
        image_count += sum(1 for b in entry_blocks if b["type"] == "image")
        blocks.extend(entry_blocks)

        prev_user = uid
        prev_dt = dt
        prev_is_user = not is_bot

    print(f"  📊 context: JSON {len(log)}条 cursor={start} session={session_len} → {len(lines)}行, {image_count}图")
    return blocks, "\n".join(lines)


async def _safe_handle_message(ws, event: dict):
    """包装 handle_message，捕获未处理异常并占住 seq，避免留下缺口。"""
    chat_id = config._chat_key(event)
    try:
        await handle_message(ws, event)
    except Exception as e:
        print(f"  ❌ handle_message 异常 [chat={chat_id}]: {e}")
        await _fill_missing_seq(chat_id, event.get("_seq", 0))


async def handle_message(ws, event: dict):
    """处理收到的消息"""
    chat_id = config._chat_key(event)
    log = storage.load_chat_log(chat_id)
    qq_to_name: dict[str, str] = {}

    for entry in reversed(log):
        uid = entry.get("user_id")
        name = entry.get("nickname", "")
        if uid and name:
            qq_to_name.setdefault(str(uid), name)

    group_id = event.get("group_id")
    if group_id:
        if storage._need_group_members(group_id):
            has_at = "[CQ:at" in (event.get("raw_message") or "")
            if not has_at:
                msg_array = event.get("message")
                if isinstance(msg_array, list):
                    has_at = any(seg.get("type") == "at" for seg in msg_array)
            if has_at:
                await storage._refresh_group_members(ws, group_id)
            else:
                await storage._refresh_group_members(ws, group_id)
        for qq, name in storage._get_group_members(group_id).items():
            qq_to_name.setdefault(qq, name)

    for b in config._ALL_BOTS:
        qq_to_name.setdefault(b.cfg.bot_qq, b.cfg.bot_name)

    raw, clean, images = await cq_utils.extract_text(event, qq_to_name, ws)
    if not raw:
        await _ordered_write(chat_id, event.get("_seq", 0), {"_cmd": True}, trigger=False)
        return

    is_group = event.get("message_type", "group") == "group"
    raw_or_msg = event.get("raw_message") or event.get("message") or ""
    is_at_bot = isinstance(raw_or_msg, str) and "[CQ:at" in raw_or_msg

    if clean.startswith("\\") and is_group and not is_at_bot:
        await _ordered_write(chat_id, event.get("_seq", 0), {"_cmd": True}, trigger=False)
        return

    # / 开头的消息（去掉前置 @后）：合法指令→执行；非法→注释（不写盘不给AI）
    cmd_test = re.sub(r'\[CQ:at[^\]]*\]', '', clean)
    cmd_test = re.sub(r'@\S+\s*', '', cmd_test, count=1).strip()
    if cmd_test.startswith("/"):
        parts = cmd_test.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if cmd in commands.COMMANDS:
            handler = commands.COMMANDS[cmd]
            targeted = config._targeted_bots(event)
            event["_targeted"] = targeted
            if not is_group or targeted:
                if args:
                    await handler(ws, event, args)
                else:
                    await handler(ws, event)
                await _ordered_write(chat_id, event.get("_seq", 0), {"_cmd": True}, trigger=False)
                return
            # 群聊中没 @ bot → 当作注释，丢弃，但要占住 seq
            await _fill_missing_seq(chat_id, event.get("_seq", 0))
            return
        else:
            # 非法 / 指令 → 注释，直接丢弃（被@也一样），占住 seq
            await _fill_missing_seq(chat_id, event.get("_seq", 0))
            return

    # 命令处理
    cmd, args = cq_utils.parse_command(clean)
    if cmd and cmd in commands.COMMANDS:
        handler = commands.COMMANDS[cmd]
        targeted = config._targeted_bots(event)
        event["_targeted"] = targeted
        if not is_group or targeted:
            if args:
                await handler(ws, event, args)
            else:
                await handler(ws, event)
            await _ordered_write(chat_id, event.get("_seq", 0), {"_cmd": True}, trigger=False)
            return
        # 群聊中没 @ bot → 不执行，当作普通消息

    await log_message(event, clean, trigger=True, images=images)


# ==================== 主入口 ====================

async def main():
    print(f"🤖 QQ Bot v{config.VERSION}")
    storage._seq_queue = asyncio.Queue()
    asyncio.create_task(storage._seq_dispatcher())

    cfg = config._load_config()
    config._apply_config(cfg)

    if config.VISION_NATIVE_IN_CHAT:
        print("🖼️ 原生多模态已启用，跳过本地视觉模型加载")
    elif config.VISION_ENABLED:
        print("Loading vision model...")
        vision._get_vision_pipe()
    else:
        print("🖼️ 视觉模型已禁用，跳过加载")

    config._ALL_BOTS = [config.BotInstance(bc) for bc in config._load_bot_configs(cfg)]
    config._BOT_QQS = {bot.cfg.bot_qq for bot in config._ALL_BOTS}

    template = config._EMPTY_SESSION_TEMPLATE
    for bot in config._ALL_BOTS:
        if template.exists() and not bot.empty_session.exists():
            shutil.copy(str(template), str(bot.empty_session))
    if template.exists() and not config._MEM_EMPTY_SESSION.exists():
        shutil.copy(str(template), str(config._MEM_EMPTY_SESSION))

    print(f"🤖 启动 {len(config._ALL_BOTS)} 个 Bot:")
    for bot in config._ALL_BOTS:
        print(f"   - {bot.cfg.bot_name} ({bot.cfg.bot_qq}): {bot.cfg.persona}")

    asyncio.create_task(storage._compact_watchdog())
    await asyncio.gather(*(bot_connect(bot) for bot in config._ALL_BOTS))


if __name__ == "__main__":
    print("=" * 40)
    print("  OneBot Multi-Bot v2.0")
    print("=" * 40)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 已退出")
