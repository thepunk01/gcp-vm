from app import integrations


class R:
    def __init__(self, d):
        self.d, self.status_code = d, 200

    def json(self):
        return self.d


def test_test_telegram(monkeypatch):
    assert integrations.test_telegram("", "1")[0] is False
    monkeypatch.setattr(integrations.requests, "post", lambda *a, **k: R({"ok": True}))
    assert integrations.test_telegram("1:x", "1") == (True, "测试消息已发送，请在 Telegram 查看")
    monkeypatch.setattr(integrations.requests, "post", lambda *a, **k: R({"ok": False, "description": "Unauthorized"}))
    ok, msg = integrations.test_telegram("1:x", "1")
    assert not ok and "Token 无效" in msg
