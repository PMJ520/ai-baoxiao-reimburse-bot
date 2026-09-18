# -*- coding: utf-8 -*-
"""本机 CLI 适配器（经宿主机 worker）。

不直连任何 API，而是把提示词丢进队列，等宿主机上的 worker 拉走、调用它那边
已登录的 CLI（claude / codex 等）、再把输出送回来。好处是复用现成登录态，
不用 API Key；代价是慢（每次要起一个 CLI 进程）且不适合并发。

worker 是单线程循环，天然串行，这里不再另加锁。
"""
import time

from .base import LLMClient, LLMError

DEFAULT_TIMEOUT = 300.0
NO_WORKER = (
    "宿主机 worker 未连接。请在装有 CLI 的机器上运行安装命令"
    "（后台「系统设置 → 对话模型」选择本机 CLI 后有对应系统的命令）。"
)


class CLIBridgeClient(LLMClient):
    def __init__(self, cfg):
        self.model = getattr(cfg, "model", "") or ""
        self.timeout = float(getattr(cfg, "timeout", 0) or DEFAULT_TIMEOUT)

    def complete(self, system, user, *, max_tokens=2048, temperature=0.2):
        from ...bridge.queue import QUEUE
        # 先看有没有 worker：没有就立刻报清楚，别让调用方干等几分钟才超时
        if not QUEUE.online():
            raise LLMError(NO_WORKER)

        prompt = f"{system.strip()}\n\n{user.strip()}" if system else user.strip()
        job = QUEUE.submit(prompt, model=self.model, max_tokens=max_tokens)
        if not job.done.wait(self.timeout):
            st = QUEUE.state()
            QUEUE.drop(job.id)
            if st.get("busy"):
                hint = "worker 还在跑这一条，只是超过了等待上限"
            elif st.get("online"):
                hint = "worker 在线但没接这一条"
            else:
                hint = "worker 已掉线"
            raise LLMError(f"等待 worker 超时（{self.timeout:.0f} 秒）：{hint}。")
        if job.error:
            raise LLMError(f"worker 执行失败：{job.error[:300]}")
        if not (job.text or "").strip():
            raise LLMError("worker 返回了空内容，请检查宿主机 CLI 是否仍处于登录状态")
        return job.text

    def probe(self):
        """设置页「测试连通性」用：区分没连上和连上了但跑不通。"""
        from ...bridge.queue import QUEUE
        if not QUEUE.online():
            return False, NO_WORKER
        env = QUEUE.state()["env"]
        who = " ".join(x for x in (env.get("os"), env.get("arch"),
                                   env.get("cli"), env.get("ver")) if x)
        t0 = time.time()
        try:
            out = self.complete("只回复两个字：正常", "测试", max_tokens=32)
        except LLMError as e:
            return False, str(e)
        return True, f"{who or 'worker'} 回复「{out.strip()[:20]}」，耗时 {time.time()-t0:.1f}s"
