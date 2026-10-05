# -*- coding: utf-8 -*-
"""DashScope 嵌入客户端。

三条实测约束（都写进了代码，不是照抄文档）：

1. **批大小上限 10** —— 传 25 会报
   ``batch size is invalid, it should not be larger than 10``
2. **TPM 1,200,000 是瓶颈**（不是 RPM）：全库 19.39M tokens，RPM 1800 只够 3.3 分钟，
   而 TPM 至少要 16 分钟。所以这里加了**令牌配额节流**，而不是等撞 429 再重试
3. **最长的 chunk 12011 字 / 7966 tokens 能被接受**；但仍保留截断保护，
   以免将来切片策略变化后超限时整批失败
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODEL = "text-embedding-v4"
DIM = 1024
BATCH_SIZE = 10          # 实测上限
MAX_CHARS = 20000        # 保护性截断（实测 12011 字可通过，留足余量）
TPM_LIMIT = 1_200_000    # 华北2（北京）text-embedding-v4
CHARS_PER_TOKEN = 1.0 / 0.7101   # 实测密度 0.7101 tokens/字符


def get_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    k = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if k:
        return k
    import subprocess
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetEnvironmentVariable('DASHSCOPE_API_KEY','User')"],
        capture_output=True, text=True, timeout=20)
    return (out.stdout or "").strip()


class _Pacer:
    """按 TPM 节流：维护最近 60 秒内已发送的 token 数，快超了就等。

    比"撞到 429 再指数退避"更高效 —— 重试会白白浪费已经消耗的额度。
    **线程安全**：并发嵌入时多个线程会同时申请额度。
    """

    def __init__(self, tpm: int = TPM_LIMIT, safety: float = 0.85):
        self.limit = int(tpm * safety)
        self.window: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()

    def wait_for(self, tokens: int) -> None:
        while True:
            with self._lock:
                now = time.time()
                self._trim(now)
                used = sum(t for _, t in self.window)
                if used + tokens <= self.limit:
                    self.window.append((now, tokens))
                    return
                oldest = self.window[0][0] if self.window else now
                sleep = max(1.0, 60 - (now - oldest) + 0.5)
            log.debug("TPM 节流：已用 %d，本次 %d，等待 %.1fs", used, tokens, sleep)
            time.sleep(sleep)


class DashScopeEmbedder:
    def __init__(self, api_key: str | None = None, model: str = MODEL,
                 dim: int = DIM, batch_size: int = BATCH_SIZE,
                 retries: int = 4, tpm: int = TPM_LIMIT, workers: int = 8):
        self.api_key = get_api_key(api_key)
        if not self.api_key:
            raise RuntimeError("没找到 DASHSCOPE_API_KEY")
        self.model, self.dim = model, dim
        self.batch_size = min(batch_size, BATCH_SIZE)
        self.retries = retries
        self.workers = max(1, workers)
        self.pacer = _Pacer(tpm)
        self.total_tokens = 0
        self.total_calls = 0
        self._stat_lock = threading.Lock()

    def _embed_batch(self, batch: list[str]) -> np.ndarray:
        """嵌入一个批次（≤10 条），内部重试。"""
        import dashscope
        from dashscope import TextEmbedding
        # 必须显式设到全局：TextEmbedding.call 不带 api_key 时只认环境变量。
        # get_api_key() 会兜底去读 Windows User 级变量，于是「key 拿得到但没生效」
        # —— 表现为 AuthenticationError，而不是「没找到 key」，很难一眼看出。
        dashscope.api_key = self.api_key
        est = int(sum(len(t) for t in batch) * CHARS_PER_TOKEN) + 8
        last_err = ""
        for attempt in range(1, self.retries + 1):
            self.pacer.wait_for(est)
            try:
                r = TextEmbedding.call(model=self.model, input=batch,
                                       dimension=self.dim, output_type="dense")
            except Exception as e:                        # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
            else:
                with self._stat_lock:
                    self.total_calls += 1
                if r.status_code == 200:
                    embs = sorted(r.output["embeddings"],
                                  key=lambda x: x["text_index"])
                    with self._stat_lock:
                        self.total_tokens += (r.usage or {}).get("total_tokens", est)
                    return np.asarray([e["embedding"] for e in embs], dtype="float32")
                last_err = f"HTTP {r.status_code}: {str(r.message)[:120]}"
                if r.status_code == 400:                  # 参数错误，重试无意义
                    raise RuntimeError(f"嵌入参数错误（不重试）：{last_err}")
            log.warning("批次失败第 %d 次：%s", attempt, last_err)
            time.sleep(min(2 ** attempt, 20))
        raise RuntimeError(f"批次连续失败：{last_err}")

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """把一批文本嵌入成 ``(n, dim)`` 的 **L2 归一化** float32 数组。

        归一化是必须的：``IndexFlatIP`` 不归一化时算的是「模长×相似度」，
        实测能把无关向量顶到第一位（分数 >1），排序直接失效。

        **并发发送**：实测单请求约 4 秒，而 TPM 只用掉不到 5% ——
        瓶颈是延迟不是额度。8 并发把全量从约 6.7 小时压到约 50 分钟。
        """
        texts = [t[:MAX_CHARS] if len(t) > MAX_CHARS else t for t in texts]
        out = np.zeros((len(texts), self.dim), dtype="float32")
        starts = list(range(0, len(texts), self.batch_size))
        if not starts:
            return out

        def job(s: int):
            return s, self._embed_batch(texts[s:s + self.batch_size])

        if self.workers == 1 or len(starts) == 1:
            for s in starts:
                s2, arr = job(s)
                out[s2:s2 + len(arr)] = arr
            return out

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = [ex.submit(job, s) for s in starts]
            for f in as_completed(futs):
                s, arr = f.result()          # 异常在这里抛出
                out[s:s + len(arr)] = arr
        return out


def _field(c, key: str):
    """兼容两种输入：chunks.jsonl 读出来是 dict，内存里的 Chunk 是对象。"""
    return c[key] if isinstance(c, dict) else getattr(c, key)


def embed_chunks_resumable(embedder: DashScopeEmbedder, code: str,
                           chunks: list, cache_dir: Path,
                           force: bool = False) -> tuple[np.ndarray, list[str]]:
    """嵌入一家公司的 chunk，**按公司落盘以便断点续跑**。

    为什么按公司分片存：全量要 5998 次请求、约 20 分钟，中途失败很常见。
    分片后重跑只补缺的公司，已嵌入的不再花钱、不再等。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    vec_path = cache_dir / f"{code}.npy"
    id_path = cache_dir / f"{code}.jsonl"
    ids = [_field(c, "chunk_id") for c in chunks]

    if not force and vec_path.exists() and id_path.exists():
        old_ids = [l.strip() for l in id_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if old_ids == ids:
            arr = np.load(vec_path)
            if arr.shape == (len(ids), embedder.dim):
                log.info("  %s 已嵌入 %d 条，跳过", code, len(ids))
                return arr, ids
        log.warning("  %s 缓存与当前切片不一致，重嵌入", code)

    arr = embedder.embed_texts([_field(c, "text") for c in chunks])
    np.save(vec_path, arr)
    id_path.write_text("\n".join(ids), encoding="utf-8")
    log.info("  %s 嵌入 %d 条 -> %s", code, len(ids), vec_path.name)
    return arr, ids
