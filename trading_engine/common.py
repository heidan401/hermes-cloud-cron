"""
共享基础模块 — feishu_push, dashscope_call, alerts, akshare helpers
云端 (GitHub Actions) 和本地 (Hermes + Claude) 同一套代码
"""

import os
import json
import time
import hashlib
import hmac
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ─── 时区 ─────────────────────────────────────────
TZ = timezone(timedelta(hours=8))


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """北京时间字符串"""
    return datetime.now(TZ).strftime(fmt)


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y%m%d")


# ─── 飞书推送 ──────────────────────────────────────

def feishu_send(title: str, body: str, app_id: str = None, app_secret: str = None,
                chat_id: str = None) -> dict:
    """发送飞书消息（纯文本，不支持 Markdown 表格）。
    
    环境变量：FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_CHAT_ID
    返回 {"success": bool, "message_id": str|None, "error": str|None}
    """
    app_id = app_id or os.getenv("FEISHU_APP_ID")
    app_secret = app_secret or os.getenv("FEISHU_APP_SECRET")
    chat_id = chat_id or os.getenv("FEISHU_CHAT_ID")

    if not all([app_id, app_secret, chat_id]):
        return {"success": False, "error": "missing feishu credentials"}

    try:
        # 1. 获取 tenant token
        ts = str(int(time.time()))
        sign = _gen_sign(ts, app_secret)

        token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        token_data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
        token_req = urllib.request.Request(token_url, data=token_data,
                                           headers={"Content-Type": "application/json"})
        token_resp = json.loads(urllib.request.urlopen(token_req).read())
        token = token_resp.get("tenant_access_token")

        if not token:
            return {"success": False, "error": f"token failed: {token_resp}"}

        # 2. 构造消息卡片（纯文本）
        content = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": body
                }
            ]
        }

        # 修复：receive_id_type 必须作为 query param 传入 URL
        msg_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        msg_data = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(content)
        }
        msg_body = json.dumps(msg_data).encode()
        msg_req = urllib.request.Request(msg_url, data=msg_body,
                                         headers={
                                             "Content-Type": "application/json",
                                             "Authorization": f"Bearer {token}"
                                         })
        try:
            msg_resp_raw = urllib.request.urlopen(msg_req).read()
            msg_resp = json.loads(msg_resp_raw)
        except urllib.error.HTTPError as he:
            err_body = he.read().decode()
            return {"success": False, "error": f"HTTP {he.code}: {err_body}"}
        msg_id = msg_resp.get("data", {}).get("message_id")

        return {"success": True, "message_id": msg_id}

    except Exception as e:
        return {"success": False, "error": str(e)}


def _gen_sign(timestamp: str, secret: str) -> str:
    """飞书签名（tenant token 用不到但保留备用）"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def feishu_alert(level: str, msg: str):
    """推送告警到飞书（简化版）。level: 🔴/🟡/🟢"""
    feishu_send(f"{level} 交易告警", msg)


# ─── DashScope LLM ─────────────────────────────────

def dashscope_chat(prompt: str, system_prompt: str = "你是A股短线交易分析师，回答精炼、有数据支撑。",
                   api_key: str = None, model: str = "qwen-plus") -> str:
    """调用通义千问（DashScope）做轻量 LLM 推理。
    云端触发器用这个，本地 Hermes 用自己的 Claude 推理。"""
    api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return "[ERROR] no DASHSCOPE_API_KEY"

    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ]
    data = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 800,
        "temperature": 0.3
    }).encode()

    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[LLM ERROR] {e}"


# ─── 时间工具 ──────────────────────────────────────

def is_trading_time() -> bool:
    """判断是否在交易时段（09:30-15:00）"""
    now = datetime.now(TZ)
    return (now.hour == 9 and now.minute >= 30) or \
           (now.hour == 10) or \
           (now.hour == 11 and now.minute <= 30) or \
           (now.hour == 13) or \
           (now.hour == 14) or \
           (now.hour == 15 and now.minute == 0)


def minutes_to_next_slot(slot_minutes: list = None) -> int:
    """距离下一个整点/半点还有几分钟"""
    if slot_minutes is None:
        slot_minutes = [0, 30]
    now = datetime.now(TZ)
    for m in sorted(slot_minutes):
        slot = now.replace(minute=m, second=0, microsecond=0)
        if slot <= now:
            slot += timedelta(minutes=30)
        return (slot - now).seconds // 60
    return 30


# ─── 跨环境文件路径 ──────────────────────────────

def get_data_file(rel_path: str) -> str:
    """跨环境解析数据文件路径。
    
    云端 (GITHUB_ACTIONS=true):
        文件放在仓库根目录，trading_engine/ 的父目录。
        如 'dragon_tracker.md' → <repo_root>/dragon_tracker.md
    
    本地:
        文件放在 ~/天才交易员/
        如 'dragon_tracker.md' → ~/天才交易员/dragon_tracker.md
    """
    if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
        # 云端：trading_engine/ 的父目录 = 仓库根
        engine_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(engine_dir)
        return os.path.join(repo_root, rel_path)
    else:
        return os.path.expanduser(f"~/天才交易员/{rel_path}")