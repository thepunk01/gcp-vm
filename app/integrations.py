"""外部集成：Telegram 通知。失败只记日志，不影响主流程。"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)



def send_telegram(settings: dict, text: str, html: bool = False) -> bool:
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
            | ({"parse_mode": "HTML"} if html else {}),
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("Telegram 通知失败：%s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as e:
        log.warning("Telegram 通知失败：%s", e)
        return False


def test_telegram(token: str, chat_id: str) -> tuple[bool, str]:
    """发送一条测试消息，返回 (是否成功, 说明)。"""
    if not token or not chat_id:
        return False, "请先填写 Telegram Bot Token 和 Chat ID"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "GCP-VM 测试通知：Telegram 通知配置正常 ✅"},
            timeout=15,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        return False, f"连接 Telegram 失败：{e}"
    if data.get("ok"):
        return True, "测试消息已发送，请在 Telegram 查看"
    desc = str(data.get("description", ""))
    hints = {
        "Unauthorized": "Bot Token 无效（可能输错，或机器人已被删除 / Token 已重置）",
        "chat not found": "找不到这个 Chat ID：确认 ID 正确，并且先给机器人发过消息，或已把机器人拉进群 / 频道",
        "bot was blocked": "你屏蔽了这个机器人，请在 Telegram 里解除屏蔽",
        "not enough rights": "机器人在群 / 频道里没有发消息权限（频道需设为管理员）",
        "bot is not a member": "机器人不在这个群 / 频道里，请先把它拉进去",
    }
    for k, v in hints.items():
        if k.lower() in desc.lower():
            return False, f"{v}（{desc}）"
    return False, f"发送失败：{desc or resp.status_code}"
