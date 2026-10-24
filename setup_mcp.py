"""
安装并注册 bilibili-mcp（供 bot 使用）。

优先使用本项目内置的 tools/bilibili-mcp 副本（离线可用）；
没有内置副本时才尝试从 GitHub 克隆。

安装完成后会检查 B 站凭证；缺失或过期时弹出二维码请求扫码登录。
设置环境变量 BILI_SKIP_LOGIN=1 可跳过登录步骤。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
VENDOR_DIR = PROJECT / "tools" / "bilibili-mcp"
TOOL_DIR = Path.home() / ".claude" / "tools" / "bilibili-mcp"
REPO = "https://github.com/InJeCTrL/bilibili-mcp"
FILES = ("mcp_server.py", "requirements.txt", "LICENSE", "README.md")
QR_FILE = PROJECT / "bilibili_login_qr.png"


def _deps_ok() -> bool:
    try:
        import mcp  # noqa: F401
        import bilibili_api  # noqa: F401
        import aiohttp  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_server() -> bool:
    if (TOOL_DIR / "mcp_server.py").exists():
        print(f"使用已安装的 bilibili-mcp: {TOOL_DIR}")
        return True

    TOOL_DIR.parent.mkdir(parents=True, exist_ok=True)

    if (VENDOR_DIR / "mcp_server.py").exists():
        print(f"使用内置 bilibili-mcp 副本: {VENDOR_DIR}")
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            src = VENDOR_DIR / name
            if src.exists():
                shutil.copy2(src, TOOL_DIR / name)
        return True

    print("未找到内置副本或已安装版本，尝试从 GitHub 克隆...")
    try:
        subprocess.run(["git", "clone", "--depth", "1", REPO, str(TOOL_DIR)], check=True)
        return True
    except Exception as e:
        print(f"❌ 克隆失败: {e}")
        print("请手动把 bilibili-mcp 放到上面的目录，或在能访问 GitHub 的网络下重试。")
        return False


def _load_credential(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        from bilibili_api import Credential
        return Credential(
            sessdata=data.get("sessdata", ""),
            bili_jct=data.get("bili_jct", ""),
            buvid3=data.get("buvid3") or "",
            dedeuserid=data.get("dedeuserid", ""),
        )
    except Exception:
        return None


async def _credential_valid(path: Path) -> bool:
    cred = _load_credential(path)
    if not cred or not cred.dedeuserid:
        return False
    try:
        from bilibili_api import user
        await user.User(uid=int(cred.dedeuserid), credential=cred).get_self_info()
        return True
    except Exception:
        return False


async def _bili_login(cred_path: Path) -> bool:
    """生成二维码并等待扫码，成功后保存凭证。"""
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

    qr = QrCodeLogin()
    await qr.generate_qrcode()
    pic = qr.get_qrcode_picture()
    QR_FILE.write_bytes(pic.content)

    print(f"\n请用 B站 App 扫描二维码登录（180 秒内有效）：{QR_FILE}")
    try:
        if os.name == "nt":
            os.startfile(str(QR_FILE))
        else:
            webbrowser.open(QR_FILE.as_uri())
    except Exception:
        pass
    url = getattr(qr, "_QrCodeLogin__qr_link", "")
    if url:
        print(f"或手动打开：{url}")

    deadline = time.time() + 180
    last_state = None
    while time.time() < deadline:
        state = await qr.check_state()
        if state == QrCodeLoginEvents.SCAN:
            if last_state != "scan":
                print("已扫码，请在手机上确认...")
                last_state = "scan"
        elif state == QrCodeLoginEvents.CONF:
            if last_state != "conf":
                print("已确认，正在登录...")
                last_state = "conf"
        elif state == QrCodeLoginEvents.TIMEOUT:
            print("⚠️ 二维码已过期")
            QR_FILE.unlink(missing_ok=True)
            return False
        elif state == QrCodeLoginEvents.DONE:
            cred = qr.get_credential()
            cred_path.parent.mkdir(parents=True, exist_ok=True)
            cred_path.write_text(json.dumps({
                "sessdata": cred.sessdata,
                "bili_jct": cred.bili_jct,
                "buvid3": cred.buvid3,
                "dedeuserid": cred.dedeuserid,
            }, ensure_ascii=False), encoding="utf-8")
            QR_FILE.unlink(missing_ok=True)
            print(f"✅ 登录成功，凭证已保存: {cred_path}")
            return True
        await asyncio.sleep(2)

    print("⚠️ 登录超时")
    QR_FILE.unlink(missing_ok=True)
    return False


def _maybe_login(cred_path: Path):
    if os.getenv("BILI_SKIP_LOGIN", "").strip() in ("1", "true", "yes"):
        print("跳过 B站登录检查（BILI_SKIP_LOGIN）")
        return
    if asyncio.run(_credential_valid(cred_path)):
        print("B站凭证有效，跳过登录")
        return
    print("B站凭证缺失或已过期，开始扫码登录...")
    if not asyncio.run(_bili_login(cred_path)):
        print("⚠️ B站登录未完成；bili_search 仍可匿名使用，详情/评论/字幕需要登录。")


def main() -> int:
    if not _ensure_server():
        return 1

    req = TOOL_DIR / "requirements.txt"
    if _deps_ok():
        print("Python 依赖已就绪，跳过 pip install")
    elif req.exists():
        print(
            "首次安装 MCP 依赖（mcp / bilibili-api-python / aiohttp），"
            "需要访问 PyPI/镜像，可能需要几分钟...",
            flush=True,
        )
        try:
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check", "--prefer-binary",
                    "--timeout", "30", "--retries", "2",
                    "-r", str(req),
                ],
                check=True,
            )
        except Exception as e:
            print(f"❌ 依赖安装失败: {e}")
            print(f"   可手动执行: {sys.executable} -m pip install -r {req}")
            return 1

    if not _deps_ok():
        print("❌ 依赖仍不可用（mcp / bilibili-api-python / aiohttp）")
        return 1

    _maybe_login(TOOL_DIR / "bili_credential.json")

    cfg_path = PROJECT / ".mcp.json"
    cfg = {
        "mcpServers": {
            "bilibili-mcp": {
                "command": sys.executable,
                "args": [str(TOOL_DIR / "mcp_server.py")],
            }
        }
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已写入 {cfg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
