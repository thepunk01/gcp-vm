from app import integrations


class R:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def run(monkeypatch, record):
    sent = {}
    monkeypatch.setattr(integrations.requests, "get", lambda *a, **k: R({"success": True, "result": [record]}))

    def patch(url, json=None, **k):
        sent.update(json)
        return R({"success": True})

    monkeypatch.setattr(integrations.requests, "patch", patch)
    integrations.update_cloudflare_a_record("t", "z", "a.example.com", "1.2.3.4")
    return sent


def test_patch_sets_min_ttl_for_dns_only(monkeypatch):
    assert run(monkeypatch, {"id": "r", "content": "9.9.9.9", "proxied": False, "ttl": 3600}) == {"content": "1.2.3.4", "ttl": 60}


def test_patch_keeps_ttl_for_proxied(monkeypatch):
    assert run(monkeypatch, {"id": "r", "content": "9.9.9.9", "proxied": True, "ttl": 1}) == {"content": "1.2.3.4"}
