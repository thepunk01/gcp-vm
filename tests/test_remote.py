import base64
import subprocess
import time

import pytest

from app.jobs import JobStore
from app.manager import Manager, ManagerError
from app.remote import (
    SSHKey,
    filter_startup_log,
    merge_ssh_keys,
    oslogin_enabled,
    unwrap_startup_script,
    wrap_startup_script,
)
from tests.conftest import FAKE_KEY, FakePool


# ---------------- 纯函数 ----------------
def test_merge_ssh_keys():
    line = "gcpvm:ssh-ed25519 AAAAnew gcpvm"
    # 空值 -> 直接写入
    assert merge_ssh_keys("", line) == line
    # 保留用户自己的公钥
    existing = "alice:ssh-rsa AAAAalice alice@pc"
    assert merge_ssh_keys(existing, line) == existing + "\n" + line
    # 已存在 -> 无需修改（注释不同也算同一把）
    assert merge_ssh_keys(existing + "\ngcpvm:ssh-ed25519 AAAAnew other-comment", line) is None
    # 同名旧密钥被替换
    merged = merge_ssh_keys(existing + "\ngcpvm:ssh-ed25519 AAAAold gcpvm", line)
    assert merged == existing + "\n" + line


def test_oslogin_enabled():
    assert not oslogin_enabled({}, {})
    assert oslogin_enabled({}, {"enable-oslogin": "TRUE"})
    assert not oslogin_enabled({"enable-oslogin": "FALSE"}, {"enable-oslogin": "TRUE"})
    assert oslogin_enabled({"enable-oslogin": "true"}, {})


def test_startup_wrap_roundtrip():
    script = "echo '你好'\nexit 0\n"
    assert wrap_startup_script(script, False) == script
    wrapped = wrap_startup_script(script, True)
    assert "run-once wrapper" in wrapped and script not in wrapped
    assert unwrap_startup_script(wrapped) == (script, True)
    assert unwrap_startup_script("echo hi") == ("echo hi", False)


@pytest.mark.skipif(subprocess.run(["which", "bash"], capture_output=True).returncode, reason="no bash")
def test_startup_wrap_runs_once(tmp_path):
    script = "echo ran >> " + str(tmp_path / "count")
    wrapped = wrap_startup_script(script, True).replace("/var/lib/gcpvm", str(tmp_path / "state"))
    for _ in range(2):
        r = subprocess.run(["bash", "-c", wrapped], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "count").read_text() == "ran\n"
    # 失败时不打标记，下次开机会重试
    bad = wrap_startup_script("exit 3", True).replace("/var/lib/gcpvm", str(tmp_path / "state2"))
    assert subprocess.run(["bash", "-c", bad], capture_output=True).returncode == 3


def test_filter_startup_log():
    log = "kernel: boot\nOct 31 google_metadata_script_runner[1]: startup-script: hello\nfoo\ngcpvm: done"
    assert filter_startup_log(log) == "Oct 31 google_metadata_script_runner[1]: startup-script: hello\ngcpvm: done"


def test_ssh_key_generation(tmp_path):
    key = SSHKey(tmp_path)
    pub = key.ensure()
    assert pub.startswith("ssh-ed25519 ")
    assert key.private_path.exists() and (key.private_path.stat().st_mode & 0o777) == 0o600
    assert SSHKey(tmp_path).ensure() == pub  # 重启后复用同一把
    assert key.metadata_line() == f"gcpvm:{pub} gcpvm"
    base64.b64decode(pub.split()[1])


def test_job_output_offsets():
    store = JobStore()
    job = store.create(1, "vm1", "ls")
    job.append("abc")
    job.append("def")
    assert job.output(0) == ("abcdef", 6)
    assert job.output(4) == ("ef", 6)
    assert store.running_for(1) is job
    job.finish("ok", 0)
    assert store.running_for(1) is None


# ---------------- manager ----------------
class FakeRunner:
    def __init__(self, code=0, output="hello\n", delay=0.0):
        self.code, self.output, self.delay = code, output, delay
        self.calls = []

    def __call__(self, host, script, key_path, on_output, **kw):
        self.calls.append({"host": host, "script": script, **kw})
        on_output(self.output)
        end = time.time() + self.delay
        while time.time() < end:
            if kw["cancelled"]():
                from app.remote import RemoteError
                raise RemoteError("已手动停止")
            time.sleep(0.01)
        return self.code


def make_manager(db, fake_gcp, tmp_path, runner):
    db.add_account("main", FAKE_KEY)
    mgr = Manager(db, FakePool(fake_gcp), ssh_key=SSHKey(tmp_path), ssh_runner=runner)
    mgr.sync_all()
    return mgr, db.list_instances()[0]["id"]


def wait_job(job, timeout=5):
    end = time.time() + timeout
    while job.status == "running" and time.time() < end:
        time.sleep(0.01)
    return job


def test_exec_writes_key_and_runs(db, fake_gcp, tmp_path):
    runner = FakeRunner()
    mgr, iid = make_manager(db, fake_gcp, tmp_path, runner)
    fake_gcp.metadata[("asia-east1-b", "vm1")] = {"ssh-keys": "alice:ssh-rsa AAAA alice"}

    job = wait_job(mgr.start_exec(iid, "uname -a\r\n"))
    assert job.status == "ok" and job.exit_code == 0
    assert "hello" in job.output()[0]
    keys = fake_gcp.metadata[("asia-east1-b", "vm1")]["ssh-keys"].splitlines()
    assert keys[0] == "alice:ssh-rsa AAAA alice" and keys[1].startswith("gcpvm:ssh-ed25519 ")
    call = runner.calls[0]
    assert call["host"] == "34.0.0.1" and call["script"] == "uname -a\n"
    assert call["auth_wait"] == 120  # 新写入公钥时等待更久

    # 第二次：公钥已存在，不再写元数据
    n = sum(1 for c in fake_gcp.calls if c[0] == "set_metadata")
    job = wait_job(mgr.start_exec(iid, "true"))
    assert sum(1 for c in fake_gcp.calls if c[0] == "set_metadata") == n
    assert runner.calls[1]["auth_wait"] == 30
    assert any(e["type"] == "exec" for e in db.list_events())


def test_exec_failure_and_guards(db, fake_gcp, tmp_path):
    mgr, iid = make_manager(db, fake_gcp, tmp_path, FakeRunner(code=2))
    job = wait_job(mgr.start_exec(iid, "false"))
    assert job.status == "failed" and job.exit_code == 2

    with pytest.raises(ManagerError):
        mgr.start_exec(iid, "   ")

    fake_gcp.project_metadata = {"enable-oslogin": "TRUE"}
    job = wait_job(mgr.start_exec(iid, "ls"))
    assert job.status == "failed" and "OS Login" in job.error

    mgr.power(iid, "stop")
    with pytest.raises(ManagerError, match="未运行"):
        mgr.start_exec(iid, "ls")


def test_exec_cancel_and_single_job(db, fake_gcp, tmp_path):
    mgr, iid = make_manager(db, fake_gcp, tmp_path, FakeRunner(delay=5))
    job = mgr.start_exec(iid, "sleep 100")
    time.sleep(0.05)
    with pytest.raises(ManagerError, match="已有命令"):
        mgr.start_exec(iid, "ls")
    job.cancel()
    wait_job(job)
    assert job.status == "cancelled"


def test_startup_script(db, fake_gcp, tmp_path):
    mgr, iid = make_manager(db, fake_gcp, tmp_path, FakeRunner())
    assert mgr.get_startup_script(iid) == {"script": "", "run_once": False, "has_script": False}

    mgr.set_startup_script(iid, "apt-get install -y curl", run_once=True, reboot=True)
    assert "run-once wrapper" in fake_gcp.metadata[("asia-east1-b", "vm1")]["startup-script"]
    assert ("reset", "vm1") in fake_gcp.calls
    assert mgr.get_startup_script(iid) == {"script": "apt-get install -y curl", "run_once": True, "has_script": True}

    # 关机状态下写入并“立即执行” -> 开机
    mgr.power(iid, "stop")
    mgr.set_startup_script(iid, "echo hi", run_once=False, reboot=True)
    assert fake_gcp.calls[-1] == ("start", "vm1")
    assert mgr.get_startup_script(iid)["run_once"] is False

    mgr.clear_startup_script(iid)
    assert "startup-script" not in fake_gcp.metadata[("asia-east1-b", "vm1")]

    fake_gcp.serial = "boot\nx startup-script: ok\n"
    assert mgr.serial_log(iid)["contents"] == "x startup-script: ok"
    assert "boot" in mgr.serial_log(iid, only_startup=False)["contents"]
