# QQ Bot (OneBot v11) — 部署指南 v2.1

## 1. 环境准备

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)（`claude` 命令可用，建议 2.1+）
- [LLOneBot](https://github.com/LLOneBot/LLOneBot) — QQ 的 OneBot v11 适配器

### 配置 Claude Code（DeepSeek）

bot 通过 Claude Code CLI 调用 DeepSeek 的 Anthropic 兼容接口。bot 使用 `--setting-sources ""` 隔离全局 Claude Code 配置，因此 **`~/.claude/settings.json` 对 bot 不生效**；请通过 `config.yaml` 或环境变量提供：

**方式 A：`config.yaml`**
```yaml
claude:
  base_url: "https://api.deepseek.com/anthropic"
  api_key: "sk-你的key"   # 不要提交到 Git
```

**方式 B：环境变量**
```bash
# Linux / macOS
export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_AUTH_TOKEN=sk-你的key
```
```bat
:: Windows
set ANTHROPIC_AUTH_TOKEN=sk-你的key
```

`bots.yaml` / `config.yaml` 已显式指定 `deepseek-flash[1M]`。

## 2. 安装依赖

```bash
pip install -r requirements.txt
python setup_mcp.py
```

`pillow` 是图片处理的必需依赖（默认装机）。本地 Qwen2-VL 的 `torch` / `transformers` 只在 `vision.native_in_chat: false` 时需要，见 `requirements-vision.txt`。

`setup_mcp.py` 用于安装并注册 B站搜索 MCP；不需要 B站搜索时可以跳过。

安装时会检查 B站凭证，缺失或过期会自动弹出二维码请求扫码登录；设置环境变量 `BILI_SKIP_LOGIN=1` 可跳过登录步骤。

全新环境首次运行 `setup_mcp.py` 会用 pip 安装 `mcp` / `bilibili-api-python` / `aiohttp`，需要能访问 PyPI 或镜像，可能耗时几分钟；也可以先手动执行 `pip install -r tools/bilibili-mcp/requirements.txt`。

## 3. 安装 LLOneBot

1. 从 [LLOneBot releases](https://github.com/LLOneBot/LLOneBot/releases) 下载最新版本
2. 按文档安装（LiteLoaderQQNT 插件方式）
3. 启动 QQ → LLOneBot 设置 → 正向 WebSocket
4. 添加 `ws://127.0.0.1:3001`，设置 access_token
5. 多 QQ 号时每个配置独立端口（3001, 3002...）

**Windows Defender 误报：** `sign-proxy.win-x64.node` 可能被隔离为 `Trojan:Win32/Kepavll!rfn`，导致 LLBot 启动报 `Cannot find module ... sign-proxy.win-x64.node`。请在 Windows 安全中心 → 保护历史记录中恢复该文件，并把 `LLBot-Desktop-win-x64` 目录加入排除项。

## 4. B站搜索（MCP，可选）

bot 只向模型暴露 persona 里列出的 7 个工具：

- `WebSearch` — 联网搜索
- `WebFetch` — 抓取指定网页
- `mcp__bilibili-mcp__bili_search` — B站视频搜索
- `mcp__bilibili-mcp__bili_video_info` — B站视频简介/标签
- `mcp__bilibili-mcp__bili_comments` — B站视频评论
- `mcp__bilibili-mcp__bili_subtitle` — B站视频字幕
- `mcp__bilibili-mcp__bili_crawl` — 批量采集（搜索+评论+字幕，需要登录）

其余工具（Bash、Edit、Read、Write、Glob、Grep、Task 等内置工具，以及其他 B 站工具）都通过 `--tools` / `--disallowedTools` 移除了。

`bili_crawl` 默认采集 3 个视频、每个 5 条评论并抓字幕；建议每视频 5~10 条评论，按需调整，避免超时和输出过大。

安装并注册 [bilibili-mcp](https://github.com/InJeCTrL/bilibili-mcp)：

```bash
python setup_mcp.py
```

脚本会：优先使用内置的 `tools/bilibili-mcp` 副本（离线可装）→ 检查/安装依赖 → 写入 `.mcp.json`（使用当前 Python 作为启动命令）。只有没有内置副本时才从 GitHub 克隆。

仓库默认的 `.mcp.json` 直接指向内置副本：

```json
{
  "mcpServers": {
    "bilibili-mcp": {
      "command": "python",
      "args": ["tools/bilibili-mcp/mcp_server.py"]
    }
  }
}
```

运行 `setup_mcp.py` 会安装依赖，并把配置改到 `~/.claude/tools/bilibili-mcp` 的独立副本（可选）。

**登录要求：**
- `bili_search` 匿名可用
- `bili_video_info`、`bili_comments`、`bili_subtitle`、`bili_crawl` 需要 `bili_credential.json`
- `setup_mcp.py` 会优先使用 `~/.claude/tools/bilibili-mcp` 里已有的安装和凭证
- 如果使用内置副本，可以把凭证复制到 `tools/bilibili-mcp/bili_credential.json`（已被 `.gitignore` 忽略）
- bot 不暴露 `bili_login`；需要登录时请用已安装的 MCP 客户端扫码，或临时移除 deny 列表中的登录工具

不需要 B站搜索时可删除 `.mcp.json`；bot 只会在文件存在时加载 MCP，此时模型只剩 `WebSearch`。

> Claude Code 本身没有“自动安装 MCP”能力；`claude mcp add` 只负责注册命令，不会克隆代码或安装依赖。`setup_mcp.py` 把这三步合在一起。

## 5. 图片识别

默认走 **原生多模态**：图片直接以 image block 进入主对话，由 `deepseek-flash[1M]` 识别，不需要本地模型。

相关配置都在 `config.yaml` 的 `vision` 段：

```yaml
vision:
  native_in_chat: true       # 原生多模态（默认）
  adaptive_resolution: true  # 按图片信息量自动选择分辨率
  resolution_simple: 512     # 简单图（扁平梗图/表情）
  resolution_normal: 768     # 普通图（照片/插画）
  resolution_dense: 1024     # 密集图（文字截图等）
  max_frames: 3              # 动图（GIF/WebP/APNG）最多抽帧数
  image_cleanup_grace: 600   # compact 清理时的图片保护期（秒）
```

图片收进来时只解码/缩放/编码一次，按内容哈希存到 `data/images/<chat_id>/`（群聊 `group-xxx`、私聊 `private-xxx` 各自独立），后续轮次复用同一份字节，配合前缀缓存可大幅降低重复图片的 token 成本。

每次 compact 推进会话窗口后，只清理**该会话目录内**已滑出窗口的图片，不影响其他群/私聊；`image_cleanup_grace`（默认 600 秒）内的新图片受保护，避免误删刚收到、尚未写入日志的图。

compact 触发的长期记忆总结同样是多模态的：被移出窗口的消息里的图片会随文字一起发给总结模型，因此图片内容也能进入长期记忆。

图片块用 `[类型：图片...]` 的形式包裹，让模型明确类型和边界：
- 普通图片：`[图片：` + image block + `]`
- 静态表情包：`[动画表情：` + image block + `]`
- 多帧普通动图：`[动图 N 帧：` + N 个 image block + `]`
- 多帧动画表情：`[动画表情 N 帧：` + N 个 image block + `]`

**旧版本地模式（可选回退）：** 设置 `native_in_chat: false` 后，图片会先由本地 Qwen2-VL 转成文字描述：

```bash
pip install -r requirements-vision.txt
```
从 [HuggingFace](https://huggingface.co/Qwen/Qwen2-VL-2B-Instruct) 下载模型放入 `models/Qwen2-VL-2B-Instruct/`，并保持 `vision.enabled: true`。

## 6. 配置 bots.yaml

```yaml
bots:
  你的QQ号:
    name: Anonther
    persona: Anonther
    admins: [你的QQ号]
    ws:
      url: ws://127.0.0.1:3001
      access_token: 你的token
    claude:
      model: deepseek-flash[1M]
```

一个 bot 对应一个 QQ 号 + 一个 ws 端口。

## 7. 人设

`persona/` 下 .md 文件即人设，文件名对应 `bots.yaml` 的 `persona`。

| 文件 | 角色 |
|------|------|
| Anonther.md | 千早爱音 — 元气吐槽役 |
| Tomoring.md | 高松灯 — 安静重女 |
| 人设参考模板.md | 新人设参考模板 |

修改人设后可在群内发 `/reload` 热重载，不需要重启 bot。

## 8. 表情包

```
stickers/<Bot名称>/
  开心/
    图片.png
  无语/
    图片.gif
  ...
```

Bot 自动从类别随机选取，以动画表情发送。

## 9. 启动

```bash
python bot.py
```

首次运行自动从 `sessions/` 模板创建 session 文件，并生成 `data/`。

## 10. 群内指令

| 指令 | 说明 |
|------|------|
| `@bot /help` | 帮助 |
| `@bot /ping` | 延迟 |
| `@bot /time` | 服务器时间 |
| `@bot /echo xxx` | 复读 |
| `@bot /roll` | 掷骰子 (1-100) |
| `@bot /calc 表达式` | 简单计算 |
| `@bot /clear [数字]` | 清除记忆（可只清最近 N 条） |
| `@bot /reload` | 热重载 |
| `@bot /ai on/off/status [--all]` | 开关/查看 AI |

## 11. 连续消息合并

构建 prompt 时，同一用户 30 秒内的连续消息会渲染成续行：第一条显示完整 `[时间] 昵称:`，后续只做缩进、不重复昵称也不显示时间（30 秒内视为同时）。

日志仍按消息逐条保存（`message_id`、图片、时间都保留），因此不影响引用查找、compact 和图片定位；合并只发生在渲染层，结果只依赖日志内容，缓存前缀稳定。
