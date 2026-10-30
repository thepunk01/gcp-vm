"""SQLite 持久化层。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    client_email TEXT NOT NULL,
    key_json     TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    project        TEXT NOT NULL,
    zone           TEXT NOT NULL,
    name           TEXT NOT NULL,
    machine_type   TEXT DEFAULT '',
    status         TEXT DEFAULT '',
    external_ip    TEXT DEFAULT '',
    internal_ip    TEXT DEFAULT '',
    network_tier   TEXT DEFAULT '',
    preemptible    INTEGER DEFAULT 0,
    -- 管理配置
    auto_ip        INTEGER DEFAULT 0,
    auto_start     INTEGER DEFAULT 0,
    check_port     INTEGER DEFAULT 22,
    ip_mode        TEXT DEFAULT 'ephemeral',
    dns_zone_id    TEXT DEFAULT '',
    dns_record     TEXT DEFAULT '',
    note           TEXT DEFAULT '',
    -- 检测状态
    cn_state       TEXT DEFAULT 'unknown',
    fail_count     INTEGER DEFAULT 0,
    last_check     REAL DEFAULT 0,
    last_change_at REAL DEFAULT 0,
    synced_at      REAL DEFAULT 0,
    UNIQUE(account_id, zone, name)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    instance_id INTEGER,
    level       TEXT NOT NULL DEFAULT 'info',
    type        TEXT NOT NULL,
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_inst ON events(instance_id, type, ts);

CREATE TABLE IF NOT EXISTS blocked_ips (
    ip          TEXT NOT NULL,
    instance_id INTEGER,
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

INSTANCE_CONFIG_FIELDS = {
    "auto_ip": int,
    "auto_start": int,
    "check_port": int,
    "ip_mode": str,
    "dns_zone_id": str,
    "dns_record": str,
    "note": str,
}


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    # ---------------- settings ----------------
    def get_settings(self) -> dict:
        result = json.loads(json.dumps(config.DEFAULT_SETTINGS))
        for row in self._all("SELECT key, value FROM settings"):
            result[row["key"]] = json.loads(row["value"])
        return result

    def update_settings(self, values: dict) -> dict:
        with self.tx() as c:
            for k, v in values.items():
                if k not in config.DEFAULT_SETTINGS:
                    continue
                c.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, json.dumps(v)),
                )
        return self.get_settings()

    # ---------------- accounts ----------------
    def add_account(self, name: str, key: dict) -> dict:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO accounts(name, project_id, client_email, key_json, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (name, key["project_id"], key["client_email"], json.dumps(key), time.time()),
            )
            return self.get_account(cur.lastrowid, with_key=False)  # type: ignore[arg-type]

    def list_accounts(self) -> list[dict]:
        return self._all(
            "SELECT id, name, project_id, client_email, created_at FROM accounts ORDER BY id"
        )

    def get_account(self, account_id: int, with_key: bool = True) -> dict | None:
        cols = "*" if with_key else "id, name, project_id, client_email, created_at"
        row = self._one(f"SELECT {cols} FROM accounts WHERE id = ?", (account_id,))
        if row and with_key:
            row["key"] = json.loads(row.pop("key_json"))
        return row

    def delete_account(self, account_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    # ---------------- instances ----------------
    def upsert_instance(self, account_id: int, data: dict) -> dict:
        """data 来自 GCP 同步：project, zone, name, machine_type, status, external_ip, ..."""
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO instances(account_id, project, zone, name, machine_type, status,
                    external_ip, internal_ip, network_tier, preemptible, synced_at)
                VALUES(:account_id, :project, :zone, :name, :machine_type, :status,
                    :external_ip, :internal_ip, :network_tier, :preemptible, :now)
                ON CONFLICT(account_id, zone, name) DO UPDATE SET
                    project = excluded.project,
                    machine_type = excluded.machine_type,
                    status = excluded.status,
                    external_ip = excluded.external_ip,
                    internal_ip = excluded.internal_ip,
                    network_tier = excluded.network_tier,
                    preemptible = excluded.preemptible,
                    synced_at = excluded.synced_at
                """,
                {
                    "account_id": account_id,
                    "project": data["project"],
                    "zone": data["zone"],
                    "name": data["name"],
                    "machine_type": data.get("machine_type", ""),
                    "status": data.get("status", ""),
                    "external_ip": data.get("external_ip", "") or "",
                    "internal_ip": data.get("internal_ip", "") or "",
                    "network_tier": data.get("network_tier", "") or "",
                    "preemptible": 1 if data.get("preemptible") else 0,
                    "now": time.time(),
                },
            )
        return self._one(
            "SELECT * FROM instances WHERE account_id = ? AND zone = ? AND name = ?",
            (account_id, data["zone"], data["name"]),
        )  # type: ignore[return-value]

    def remove_missing_instances(self, account_id: int, keep_ids: list[int]) -> int:
        with self.tx() as c:
            if keep_ids:
                marks = ",".join("?" * len(keep_ids))
                cur = c.execute(
                    f"DELETE FROM instances WHERE account_id = ? AND id NOT IN ({marks})",
                    (account_id, *keep_ids),
                )
            else:
                cur = c.execute("DELETE FROM instances WHERE account_id = ?", (account_id,))
            return cur.rowcount

    def list_instances(self) -> list[dict]:
        return self._all(
            "SELECT i.*, a.name AS account_name FROM instances i "
            "JOIN accounts a ON a.id = i.account_id ORDER BY a.id, i.zone, i.name"
        )

    def get_instance(self, instance_id: int) -> dict | None:
        return self._one(
            "SELECT i.*, a.name AS account_name FROM instances i "
            "JOIN accounts a ON a.id = i.account_id WHERE i.id = ?",
            (instance_id,),
        )

    def update_instance(self, instance_id: int, **fields: Any) -> dict | None:
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self.tx() as c:
                c.execute(
                    f"UPDATE instances SET {cols} WHERE id = ?", (*fields.values(), instance_id)
                )
        return self.get_instance(instance_id)

    def update_instance_config(self, instance_id: int, values: dict) -> dict | None:
        clean: dict[str, Any] = {}
        for k, cast in INSTANCE_CONFIG_FIELDS.items():
            if k in values and values[k] is not None:
                v = values[k]
                clean[k] = int(bool(v)) if k in ("auto_ip", "auto_start") else cast(v)
        if "ip_mode" in clean and clean["ip_mode"] not in ("ephemeral", "static"):
            raise ValueError("ip_mode 只能是 ephemeral 或 static")
        if "check_port" in clean and not (1 <= clean["check_port"] <= 65535):
            raise ValueError("端口范围 1-65535")
        return self.update_instance(instance_id, **clean)

    # ---------------- events ----------------
    def add_event(
        self, type_: str, message: str, instance_id: int | None = None, level: str = "info"
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events(ts, instance_id, level, type, message) VALUES(?, ?, ?, ?, ?)",
                (time.time(), instance_id, level, type_, message),
            )

    def list_events(self, limit: int = 200, instance_id: int | None = None) -> list[dict]:
        if instance_id is not None:
            return self._all(
                "SELECT e.*, i.name AS instance_name FROM events e "
                "LEFT JOIN instances i ON i.id = e.instance_id "
                "WHERE e.instance_id = ? ORDER BY e.id DESC LIMIT ?",
                (instance_id, limit),
            )
        return self._all(
            "SELECT e.*, i.name AS instance_name FROM events e "
            "LEFT JOIN instances i ON i.id = e.instance_id ORDER BY e.id DESC LIMIT ?",
            (limit,),
        )

    def count_events_since(self, instance_id: int, type_: str, since: float) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM events WHERE instance_id = ? AND type = ? AND ts >= ?",
            (instance_id, type_, since),
        )
        return int(row["n"]) if row else 0

    # ---------------- blocked ips ----------------
    def add_blocked_ip(self, ip: str, instance_id: int | None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO blocked_ips(ip, instance_id, ts) VALUES(?, ?, ?)",
                (ip, instance_id, time.time()),
            )

    def recent_blocked_ips(self, days: int = 30) -> set[str]:
        since = time.time() - days * 86400
        return {r["ip"] for r in self._all("SELECT ip FROM blocked_ips WHERE ts >= ?", (since,))}
