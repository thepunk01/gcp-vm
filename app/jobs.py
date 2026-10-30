"""后台命令任务：保存执行输出，供前端按偏移量轮询。"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field

MAX_OUTPUT_CHARS = 1_000_000
KEEP_JOBS = 50


@dataclass
class Job:
    id: int
    instance_id: int
    instance_name: str
    script: str
    status: str = "running"  # running / ok / failed / cancelled
    exit_code: int | None = None
    error: str = ""
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    truncated: bool = False
    _chunks: list[str] = field(default_factory=list, repr=False)
    _size: int = 0
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            room = MAX_OUTPUT_CHARS - self._size
            if room <= 0:
                self.truncated = True
                return
            if len(text) > room:
                text = text[:room]
                self.truncated = True
            self._chunks.append(text)
            self._size += len(text)

    def output(self, offset: int = 0) -> tuple[str, int]:
        with self._lock:
            full = "".join(self._chunks)
            self._chunks = [full] if full else []
        return full[offset:], len(full)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def finish(self, status: str, exit_code: int | None = None, error: str = "") -> None:
        self.status, self.exit_code, self.error, self.ended = status, exit_code, error, time.time()

    def summary(self) -> dict:
        return {
            "id": self.id, "instance_id": self.instance_id, "instance_name": self.instance_name,
            "status": self.status, "exit_code": self.exit_code, "error": self.error,
            "started": self.started, "ended": self.ended, "truncated": self.truncated,
            "script": self.script[:2000],
        }


class JobStore:
    def __init__(self):
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def create(self, instance_id: int, instance_name: str, script: str) -> Job:
        with self._lock:
            job = Job(next(self._ids), instance_id, instance_name, script)
            self._jobs[job.id] = job
            # 只保留最近的任务（运行中的不清理）
            done = [j for j in self._jobs.values() if j.status != "running"]
            for old in sorted(done, key=lambda j: j.id)[: max(0, len(self._jobs) - KEEP_JOBS)]:
                self._jobs.pop(old.id, None)
            return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def running_for(self, instance_id: int) -> Job | None:
        return next(
            (j for j in self._jobs.values() if j.instance_id == instance_id and j.status == "running"),
            None,
        )

    def list(self, instance_id: int | None = None) -> list[Job]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)
        return [j for j in jobs if instance_id is None or j.instance_id == instance_id]
