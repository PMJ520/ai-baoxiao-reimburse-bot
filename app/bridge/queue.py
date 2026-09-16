# -*- coding: utf-8 -*-
"""宿主机 worker 的任务队列。

方向是反的：不是容器去调宿主机，而是宿主机上的 worker 主动来长轮询取活。
这样宿主机不用监听端口、不用被容器寻址，worker 甚至可以跑在另一台机器上。

放内存不落库：任务是秒级的，进程重启时正在跑的那条丢掉也无所谓，
下次调用重新排队即可。服务以单进程 uvicorn 运行，不存在多进程共享问题。
"""
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

# worker 每次长轮询最多挂 30 秒，所以超过这个数还没露面就是掉线了
ONLINE_WINDOW = 75.0


@dataclass
class Job:
    id: str
    prompt: str
    model: str = ""
    max_tokens: int = 2048
    created: float = field(default_factory=time.time)
    done: threading.Event = field(default_factory=threading.Event)
    text: str = ""
    error: str = ""


class JobQueue:
    def __init__(self):
        self._pending = queue.Queue()
        self._inflight = {}
        self._lock = threading.Lock()
        self._worker = {}          # 最近一次拉取时上报的环境
        self._last_seen = 0.0

    # ---- 调用方（LLM 适配器）----

    def submit(self, prompt, model="", max_tokens=2048):
        job = Job(id=uuid.uuid4().hex, prompt=prompt, model=model,
                  max_tokens=max_tokens)
        with self._lock:
            self._inflight[job.id] = job
        self._pending.put(job)
        return job

    def drop(self, job_id):
        """调用方等超时后清理，避免 worker 迟到的结果堆在内存里。"""
        with self._lock:
            self._inflight.pop(job_id, None)

    # ---- worker 侧 ----

    def take(self, wait, env=None):
        """长轮询取一条任务；wait 秒内没有就返回 None。"""
        self.touch(env)
        deadline = time.time() + max(0.0, wait)
        while True:
            try:
                job = self._pending.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                return None
            # 调用方可能已经超时撤单，跳过这种任务继续取，别把它派给 worker
            with self._lock:
                if job.id in self._inflight:
                    return job

    def finish(self, job_id, text="", error=""):
        with self._lock:
            job = self._inflight.pop(job_id, None)
        if not job:
            return False
        job.text, job.error = text, error
        job.done.set()
        return True

    def touch(self, env=None):
        with self._lock:
            self._last_seen = time.time()
            if env:
                self._worker = dict(env)

    # ---- 状态 ----

    def online(self):
        return (time.time() - self._last_seen) < ONLINE_WINDOW

    def state(self):
        with self._lock:
            env, seen = dict(self._worker), self._last_seen
        return {"online": self.online(), "last_seen": seen,
                "idle": int(time.time() - seen) if seen else None,
                "env": env, "pending": self._pending.qsize()}


QUEUE = JobQueue()
