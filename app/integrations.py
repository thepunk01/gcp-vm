"""外部集成：Telegram 通知、Cloudflare DNS 更新。失败只记日志，不影响主流程。"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

CF_API = "https://api.cloudflare.com/client/v4"


def send_telegram(settings: dict, text: str) -> bool:
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("Telegram 通知失败：%s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as e:
        log.warning("Telegram 通知失败：%s", e)
        return False


class DNSError(Exception):
    pass


def update_cloudflare_a_record(api_token: str, zone_id: str, record_name: str, ip: str) -> str:
    """把 A 记录 record_name 指向 ip；记录不存在则创建。返回执行的动作描述。"""
    if not (api_token and zone_id and record_name):
        raise DNSError("Cloudflare 配置不完整（需要 API Token、Zone ID 与记录名）")
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        resp = requests.get(
            f"{CF_API}/zones/{zone_id}/dns_records",
            params={"type": "A", "name": record_name},
            headers=headers,
            timeout=15,
        )
        data = resp.json()
        if not data.get("success"):
            raise DNSError(f"查询 DNS 记录失败：{data.get('errors')}")
        records = data.get("result") or []
        if records:
            rec = records[0]
            if rec.get("content") == ip:
                return f"{record_name} 已指向 {ip}"
            patch: dict = {"content": ip}
            if not rec.get("proxied"):
                # 换 IP 场景需要尽快生效：未开启代理（灰云）的记录统一把 TTL 调到最小 60 秒
                patch["ttl"] = 60
            resp = requests.patch(
                f"{CF_API}/zones/{zone_id}/dns_records/{rec['id']}",
                json=patch,
                headers=headers,
                timeout=15,
            )
            action = "更新"
        else:
            resp = requests.post(
                f"{CF_API}/zones/{zone_id}/dns_records",
                json={"type": "A", "name": record_name, "content": ip, "ttl": 60, "proxied": False},
                headers=headers,
                timeout=15,
            )
            action = "创建"
        data = resp.json()
        if not data.get("success"):
            raise DNSError(f"{action} DNS 记录失败：{data.get('errors')}")
        return f"已{action} {record_name} -> {ip}"
    except (requests.RequestException, ValueError) as e:
        raise DNSError(f"Cloudflare 请求失败：{e}") from e
