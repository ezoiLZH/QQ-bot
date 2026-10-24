"""
持久化模块 —— 聊天记录、游标、长期记忆、Session Compact、总结
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import shutil
from datetime import datetime
from pathlib import Path

import config
import media_store


# ==================== 有序写入（共享） ====================
_chat_seq: dict[str, int] = {}                      # chat_id → 下一个序号
_pending_writes: dict[str, dict[int, dict]] = {}    # chat_id → {seq: 日志条目}
_next_write_seq: dict[str, int] = {}                # chat_id → 下一个期望写入的 seq
_session_cursor: dict[str, int] = {}                # chat_id → 会话起始索引
_write_flush_lock = asyncio.Lock()
_pending_triggers: dict[str, set[int]] = {}          # chat_id → 需要触发处理的 seq 集合
_summary_lock = asyncio.Lock()
_summarizing: set[str] = set()

# ── Seq 分配 ──
_seq_queue: asyncio.Queue | None = None

# 群成员缓存
_group_members: dict[int, dict[str, str]] = {}
_group_members_time: dict[int, float] = {}
_group_refreshing: set[int] = set()
_pending_api: dict[str, asyncio.Future] = {}
_echo_counter = 0


# ==================== API 工具 ====================

def _new_echo() -> str:
    global _echo_counter
    _echo_counter += 1
    return f"bot_{_echo_counter}"


def _handle_api_response(data: dict):
    echo = data.get("echo", "")
    if echo:
        if echo in _pending_api:
            fut = _pending_api.pop(echo)
            fut.set_result(data)
        else:
            print(f"⚠️ API 响应 echo={echo} 未找到对应的 Future")
    elif "status" in data:
        pass  # heartbeat/ack 静默忽略


async def _api_call(ws, action: str, params: dict, timeout: float = 10) -> dict:
    echo = _new_echo()
    payload = {"action": action, "params": params, "echo": echo}
    fut = asyncio.get_running_loop().create_future()
    _pending_api[echo] = fut
    await ws.send(json.dumps(payload))
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending_api.pop(echo, None)
        return {"status": "timeout", "retcode": -1}


# ==================== 群成员缓存 ====================

def _get_group_members(group_id: int) -> dict[str, str]:
    return _group_members.get(group_id, {})


def _need_group_members(group_id: int) -> bool:
    if group_id in _group_refreshing:
        return False
    if group_id not in _group_members:
        return True
    return (time.time() - _group_members_time.get(group_id, 0)) >= config._GROUP_MEMBER_TTL


async def _refresh_group_members(ws, group_id: int):
    _group_refreshing.add(group_id)
    try:
        resp = await _api_call(ws, "get_group_member_list", {"group_id": group_id})
        count = len(resp.get("data", []))
        print(f"  🔄 刷新群成员: 群{group_id} → {count}人")
        if resp.get("status") == "ok":
            members: dict[str, str] = {}
            for m in resp.get("data", []):
                qq = str(m.get("user_id", ""))
                name = m.get("card") or m.get("nickname") or ""
                if qq and name:
                    members[qq] = name
            _group_members[group_id] = members
            _group_members_time[group_id] = time.time()
    finally:
        _group_refreshing.discard(group_id)


# ==================== Seq 分配 ====================

async def _request_seq(chat_id: str) -> int:
    q = _seq_queue
    if q is None:
        _chat_seq[chat_id] = _chat_seq.get(chat_id, 0) + 1
        return _chat_seq[chat_id]
    evt = asyncio.Event()
    result: dict = {}
    await q.put((chat_id, evt, result))
    try:
        await asyncio.wait_for(evt.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        # 标记作废，避免 dispatcher 稍后处理时多消耗一个 seq 造成缺口
        result["cancelled"] = True
        print(f"  ⚠️ seq dispatcher 超时，fallback 直接递增")
        _chat_seq[chat_id] = _chat_seq.get(chat_id, 0) + 1
        return _chat_seq[chat_id]
    return result["seq"]


async def _seq_dispatcher():
    q = _seq_queue
    if q is None:
        return
    while True:
        try:
            chat_id, evt, result = await q.get()
            if result.get("cancelled"):
                continue  # 请求方已超时并自行分配了 seq
            _chat_seq[chat_id] = _chat_seq.get(chat_id, 0) + 1
            result["seq"] = _chat_seq[chat_id]
            evt.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  ❌ seq dispatcher 异常: {e}")


# ==================== 聊天记录持久化 ====================

def chat_file_path(chat_id: str) -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', chat_id)
    return config.DATA_DIR / f"{safe}.json"


def load_chat_log(chat_id: str) -> list[dict]:
    path = chat_file_path(chat_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _load_cursors() -> dict[str, int]:
    if config.CURSOR_FILE.exists():
        try:
            return json.loads(config.CURSOR_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cursors():
    config.CURSOR_FILE.write_text(
        json.dumps(_session_cursor, ensure_ascii=False),
        encoding="utf-8",
    )


_cursors_loaded = False


def _ensure_cursors_loaded():
    global _cursors_loaded
    if not _cursors_loaded:
        _cursors_loaded = True
        saved = _load_cursors()
        _session_cursor.update(saved)


def _set_cursor(chat_id: str, value: int):
    _session_cursor[chat_id] = value
    _save_cursors()


def save_chat_log(chat_id: str, log: list[dict]):
    _ensure_cursors_loaded()
    keep = config.LOG_MAX_ENTRIES
    removed = 0
    if len(log) > keep:
        removed = len(log) - keep
        log = log[-keep:]

    if removed > 0 and chat_id in _session_cursor:
        _set_cursor(chat_id, max(0, _session_cursor[chat_id] - removed))

    chat_file_path(chat_id).write_text(
        json.dumps(log, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ==================== 图片清理 ====================

_image_cleanup_lock = threading.Lock()


def _active_image_files(chat_id: str) -> set[str] | None:
    """返回该聊天当前会话窗口引用的图片路径；日志读取失败返回 None。"""
    path = chat_file_path(chat_id)
    if not path.exists():
        return set()
    try:
        log = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(log, list):
        return None
    start = _session_cursor.get(chat_id, 0)
    active: set[str] = set()
    for entry in log[start:]:
        if not isinstance(entry, dict):
            continue
        for meta in entry.get("images", []) or []:
            active.update(meta.get("files", []) or [])
    return active


def cleanup_inactive_images(chat_id: str | None = None,
                            grace_seconds: int | None = None) -> tuple[int, int]:
    """删除不在当前会话窗口内的图片文件。

    chat_id 指定时只清理该会话的目录（compact 后调用）；None 时清理所有会话。
    只删除 mtime 超过保护期的文件，避免误删刚收下、尚未写入日志的图片。
    返回 (删除数, 释放字节数)。
    """
    if not _image_cleanup_lock.acquire(blocking=False):
        return 0, 0
    try:
        _ensure_cursors_loaded()
        grace = config.VISION_IMAGE_CLEANUP_GRACE if grace_seconds is None else grace_seconds
        now = time.time()

        if chat_id:
            chat_ids = [chat_id]
        else:
            chat_ids = [
                p.stem for p in config.DATA_DIR.glob("*.json")
                if p.stem.startswith("group-") or p.stem.startswith("private-")
            ]

        deleted = freed = 0
        for cid in chat_ids:
            active = _active_image_files(cid)
            if active is None:
                print(f"  🧹 图片清理跳过 {cid}（日志读取失败）")
                continue

            safe = media_store.safe_chat_id(cid)
            img_dir = config.DATA_DIR / media_store.IMAGES_DIR_NAME / safe
            if not img_dir.exists():
                continue
            for f in img_dir.glob("*.jpg"):
                if f"{media_store.IMAGES_DIR_NAME}/{safe}/{f.name}" in active:
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if now - st.st_mtime < grace:
                    continue
                try:
                    f.unlink()
                    deleted += 1
                    freed += st.st_size
                except OSError:
                    pass

        if deleted:
            print(f"🧹 图片清理: 删除 {deleted} 个文件，释放 {freed / 1024 / 1024:.1f} MB")
        return deleted, freed
    finally:
        _image_cleanup_lock.release()


# ==================== 长期记忆 ====================

def _memory_path(chat_id: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|]', "_", chat_id)
    return config.DATA_DIR / f"memory-{safe}.json"


def load_memory(chat_id: str) -> list[dict]:
    path = _memory_path(chat_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_memory(chat_id: str, entries: list[dict]):
    entries.sort(key=lambda e: e.get("time", ""))
    hard_limit = config.MEMORY_MAX_ENTRIES + 5
    if len(entries) > hard_limit:
        entries = entries[-hard_limit:]
    path = _memory_path(chat_id)
    # 先备份再写入
    if path.exists():
        bak = path.with_suffix(".json.bak")
        shutil.copy2(path, bak)
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def format_memory_for_prompt(chat_id: str) -> str:
    entries = load_memory(chat_id)
    if not entries:
        return ""
    lines = ["【长期记忆 — 之前聊过的重要内容】"]
    for e in entries:
        ts = e.get("time", "?")
        summary = e.get("summary", "")
        summary = summary.replace("🤖", "")
        lines.append(f"- [{ts}] {summary}")
    return "\n".join(lines)


# ==================== 记忆总结 ====================

async def _call_claude_one_shot(content: str | list[dict]) -> str | None:
    """调用 Claude Code 做一次性总结。content 可以是纯文本或 content blocks（含图片）。"""
    api_key = config.CLAUDE_API_KEY
    if isinstance(content, str):
        content_blocks = [{"type": "text", "text": content}]
    else:
        content_blocks = content

    cmd = [
        config._claude_exe,
        "-p",
        "--input-format", "stream-json",
        "--model", config.CLAUDE_MEMORY_MODEL,
        "--max-turns", "1",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--resume", config._MEM_SESSION_ID,
        "--bare",
        "--tools", "",
        "--disable-slash-commands",
    ]
    settings_override = config.claude_settings_json(api_key)
    if settings_override:
        cmd += ["--settings", settings_override]
    proc = None
    try:
        import os
        env = os.environ.copy()
        env.pop("ENABLE_TOOL_SEARCH", None)
        env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"
        env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"

        # 每次总结从空模板开始，避免 session 无限膨胀
        if config._MEM_EMPTY_SESSION.exists():
            config._MEM_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(str(config._MEM_EMPTY_SESSION), str(config._MEM_SESSION_FILE))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(config.BASE_DIR),
        )
        payload = json.dumps(
            {"type": "user", "message": {"role": "user", "content": content_blocks}},
            ensure_ascii=False,
        ) + "\n"
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")),
            timeout=config.CLAUDE_MEMORY_TIMEOUT,
        )
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:200] if stderr else ""
            print(f"  ⚠️ 记忆总结失败: {err}")
            return None

        # stream-json 输出：取最后一个 result 事件
        data: dict = {}
        result_text = ""
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                data = event

        result_text = (data.get("result") or "").strip()
        usage = data.get("usage", {})
        if usage:
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cache_hit = usage.get("cache_read_input_tokens") or usage.get("prompt_cache_hit_tokens", 0)
            total_context = input_tokens + cache_hit
            hit_rate = cache_hit / total_context * 100 if total_context > 0 else 0
            cost = data.get("total_cost_usd", 0)
            print(f"  💾 总结用量: in:{input_tokens}(new) out:{output_tokens} "
                  f"cache: hit={cache_hit} total={total_context} rate={hit_rate:.0f}% "
                  f"${cost:.4f}")

        return result_text if result_text else None
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        print(f"  ⚠️ 记忆总结超时")
        return None
    except Exception as e:
        print(f"  ⚠️ 记忆总结异常: {e}")
        return None


async def _submit_compact(chat_id: str, reason: str):
    """compact 总入口：锁防并发 → 计算 cursor → 总结 → 更新记忆和 cursor。"""
    async with _summary_lock:
        if chat_id in _summarizing:
            return
        _summarizing.add(chat_id)

    compacted = False
    try:
        log = load_chat_log(chat_id)
        _ensure_cursors_loaded()
        if chat_id not in _session_cursor:
            # 无历史 cursor：保留 compact_to + overlap 条作为初始会话窗口
            initial_window = config.SESSION_COMPACT_TO + config.SESSION_OVERLAP
            _set_cursor(chat_id, max(0, len(log) - initial_window))
        elif _session_cursor[chat_id] > len(log):
            # 日志被截断后 cursor 失效
            _set_cursor(chat_id, max(0, len(log) - config.SESSION_COMPACT_TO))
        start = _session_cursor[chat_id]
        cutoff = max(0, len(log) - config.SESSION_COMPACT_TO)
        cursor_target = max(start, cutoff - config.SESSION_OVERLAP)
        print(f"  📦 compact [{chat_id}]: cursor {start}→{cursor_target}, cutoff={cutoff}, {reason}")

        summary_end = min(len(log), cutoff + config.SESSION_OVERLAP)
        removed = log[start:summary_end]
        removed_count = len(removed)

        if removed_count < config.MEMORY_MIN_COMPACT:
            compacted = True
            return

        # 构建总结 prompt（图片与聊天行交错，让总结模型也能看到图片语义）
        bot_names: set[str] = {b.cfg.bot_name for b in config._ALL_BOTS if b.persona_text}
        lines = []
        removed_blocks: list[dict] = []
        prev_user: str | None = None
        prev_dt = None
        prev_is_user = False
        for idx, entry in enumerate(removed):
            ts = entry.get("time", "")
            name = entry.get("nickname", "?")
            msg = entry.get("message", "")
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

            full_prefix = f"[{ts}] {'🤖' if is_bot else ''}{name}: "
            if can_merge:
                display_prefix = media_store.CONTINUATION_PREFIX
                block_prefix = "\n" + display_prefix
            else:
                display_prefix = full_prefix
                block_prefix = ("" if idx == 0 else "\n") + display_prefix
            lines.append(display_prefix + msg)
            removed_blocks.extend(
                media_store.build_blocks(msg, entry.get("images") or [], prefix=block_prefix)
            )

            prev_user = uid
            prev_dt = dt
            prev_is_user = not is_bot
        removed_text = "\n".join(lines)
        bot_hint = ""
        if bot_names:
            bot_list = "、".join(sorted(bot_names))
            bot_hint = f"⚠️ 当前机器人为：{bot_list}。（详见模板中的「机器人识别」规则）\n\n"

        template_path = config.DATA_DIR / "_summary_prompt_template.txt"
        try:
            summary_prompt = template_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"  ⚠️ 记忆总结模板不存在: {template_path}")
            return
        summary_prompt = summary_prompt.replace("{max_entries}", str(config.MEMORY_MAX_ENTRIES))
        summary_prompt += "\n\n## 现有长期记忆\n"
        existing = load_memory(chat_id)
        existing_text = ""
        if existing:
            for e in existing:
                ts = e.get("time", "?")
                s = e.get("summary", "")
                line = f"- [{ts}] {s}"
                summary_prompt += line + "\n"
                existing_text += line + "\n"
        else:
            summary_prompt += "（无）\n"
            existing_text = "（无）"
        summary_prompt += (
            "\n\n## 新聊天内容（刚从会话窗口移除）\n"
            f"{bot_hint}"
        )
        summary_content = [{"type": "text", "text": summary_prompt}] + removed_blocks

        debug_text = existing_text + "\n\n" + removed_text
        (config.DATA_DIR / "_last_summary_input.txt").write_text(debug_text, encoding="utf-8")

        summary = await _call_claude_one_shot(summary_content)
        if not summary:
            return

        # 解析总结结果
        parsed: list[dict] = []
        _TS_RE = re.compile(r'\[(\d{1,2}-\d{1,2}\s+\d{1,2})\]')
        for line in summary.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            line = re.sub(r'^[-*+]\s+', '', line)
            line = re.sub(r'^\d+[.)]\s*', '', line)
            line = re.sub(r'^[`](.*)[`]$', r'\1', line)
            line = line.strip('`')
            line = re.sub(r'^\*\*(.+?)\*\*$', r'\1', line)
            line = re.sub(r'^__(.+?)__$', r'\1', line)
            line = line.strip()

            # 按时间戳拆分成多条
            parts = _TS_RE.split(line)
            # parts = ['前导文本', 'ts1', 'text1', 'ts2', 'text2', ...]
            # 跳过前导文本(parts[0])，按 (ts, text) 配对
            for i in range(1, len(parts) - 1, 2):
                ts = parts[i]
                text = parts[i + 1].strip()
                text = re.sub(r'\s{2,}', ' ', text)
                text = text.rstrip('；;，,。.')
                if text:
                    parsed.append({"time": ts, "summary": text})

        if parsed:
            save_memory(chat_id, parsed)
            compacted = True
            print(f"  🧠 长期记忆 [{chat_id}]: {len(parsed)}条 (compact {removed_count}条 → 总结合并)")
        else:
            print(f"  ❌ 记忆解析失败 [{chat_id}]，不写 memory，不 compact，下次重试")

    finally:
        _summarizing.discard(chat_id)
        if compacted:
            current = _session_cursor.get(chat_id, 0)
            # compact 期间 save_chat_log 截断可能降了 cursor
            # cursor_target 基于旧 log 索引，减去位移量保证指向同一条消息
            if current < start:
                cursor_target -= (start - current)
            if cursor_target > current:
                session_len = len(load_chat_log(chat_id)) - current
                _set_cursor(chat_id, cursor_target)
                print(f"  ✅ compact [{chat_id}]: cursor → {cursor_target}, "
                      f"session {session_len}→{len(load_chat_log(chat_id))-cursor_target}")
            # compact 后清理已滑出会话窗口的图片
            try:
                await asyncio.to_thread(cleanup_inactive_images, chat_id)
            except Exception as e:
                print(f"  ⚠️ compact 后图片清理异常: {e}")
        elif not compacted:
            print(f"  ⚠️ compact 失败 [{chat_id}]，下次重试")


# ==================== Session Compact 检查 ====================

def _check_hard_limit(chat_id: str):
    """写盘/读盘时：session ≥ HARD_LIMIT → 提交 compact"""
    log = load_chat_log(chat_id)
    _ensure_cursors_loaded()
    start = _session_cursor.get(chat_id, 0)
    if len(log) - start >= config.SESSION_HARD_LIMIT:
        asyncio.ensure_future(_submit_compact(chat_id, f"hard limit (≥{config.SESSION_HARD_LIMIT})"))


def _check_soft_limit(chat_id: str):
    """Watchdog：session ≥ SOFT_LIMIT + 空闲 → 提交 compact"""
    log = load_chat_log(chat_id)
    _ensure_cursors_loaded()
    start = _session_cursor.get(chat_id, 0)
    if len(log) - start < config.SESSION_SOFT_LIMIT:
        return
    last_reply = max((b.last_reply_time.get(chat_id, 0) for b in config._ALL_BOTS), default=0)
    if time.time() - last_reply > config.SESSION_IDLE_SECONDS:
        asyncio.ensure_future(_submit_compact(chat_id, f"idle (≥{config.SESSION_SOFT_LIMIT})"))


# ==================== 后台任务 ====================

async def _compact_watchdog():
    """后台任务：定期检查所有活跃会话的空闲 compact。"""
    while True:
        await asyncio.sleep(60)
        try:
            for chat_id in list(_session_cursor.keys()):
                _check_soft_limit(chat_id)
        except Exception as e:
            print(f"  ⚠️ compact watchdog 异常: {e}")
