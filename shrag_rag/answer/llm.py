# -*- coding: utf-8 -*-
"""DashScope LLM 客户端（问答生成用）。

实测：``qwen-turbo-latest`` 返回 **403 Access denied**，而
``qwen-plus`` / ``qwen-turbo`` / ``qwen-flash`` / ``qwen-max`` / ``qwen3-max``
都正常。默认用 ``qwen-plus`` —— 财务问答对指令遵循要求较高，
turbo 档在这类"必须按证据回答、必须标引用"的任务上容易走样。
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen-plus"


class DashScopeLLM:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 temperature: float = 0.1, retries: int = 3, timeout: int = 120):
        from ..index.embedder import get_api_key
        self.api_key = get_api_key(api_key)
        self.model = model
        # 财务问答要可复现、少发挥，温度压低
        self.temperature = temperature
        self.retries = retries
        self.timeout = timeout
        self.total_in = 0
        self.total_out = 0

    def chat(self, messages: list[dict], **kw) -> str:
        import dashscope
        from dashscope import Generation
        dashscope.api_key = self.api_key

        # 参数在循环外取一次：写在 pop 里的话，重试那一次会丢掉调用方传的 model
        model = kw.pop("model", self.model)
        temperature = kw.pop("temperature", self.temperature)

        last = ""
        for attempt in range(1, self.retries + 1):
            try:
                r = Generation.call(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    result_format="message",
                    **kw)
            except Exception as e:                       # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    u = r.usage or {}
                    self.total_in += u.get("input_tokens", 0)
                    self.total_out += u.get("output_tokens", 0)
                    return r.output["choices"][0]["message"]["content"].strip()
                last = f"HTTP {r.status_code}: {str(r.message)[:150]}"
                if r.status_code in (400, 401, 403):     # 参数/权限问题，重试无意义
                    raise RuntimeError(f"LLM 调用失败（不重试）：{last}")
            log.warning("LLM 第 %d 次失败：%s", attempt, last)
            time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM 连续失败：{last}")

    def chat_stream(self, messages: list[dict], **kw):
        """流式生成，逐片 yield 文本增量。

        实测 qwen-plus 非流式要 1813 ms 才出全文，流式首字 347 ms。
        问答一题本来就要 3~7 秒，不做流式页面会一直干等。

        已经吐出过内容后再出错就**不重试**了 —— 重试会把前半段重复一遍。
        """
        import dashscope
        from dashscope import Generation
        dashscope.api_key = self.api_key

        model = kw.pop("model", self.model)
        temperature = kw.pop("temperature", self.temperature)

        last = ""
        for attempt in range(1, self.retries + 1):
            got = False
            try:
                rsp = Generation.call(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    result_format="message",
                    stream=True,
                    incremental_output=True,
                    **kw)
                for r in rsp:
                    if r.status_code != 200:
                        last = f"HTTP {r.status_code}: {str(r.message)[:150]}"
                        if r.status_code in (400, 401, 403):
                            raise RuntimeError(f"LLM 流式调用失败（不重试）：{last}")
                        break
                    piece = r.output["choices"][0]["message"]["content"] or ""
                    u = r.usage or {}
                    if u:
                        self.total_in += u.get("input_tokens") or 0
                        self.total_out += u.get("output_tokens") or 0
                    if piece:
                        got = True
                        yield piece
                else:
                    return                                # 正常读完
            except RuntimeError:
                raise
            except Exception as e:                        # noqa: BLE001
                last = f"{type(e).__name__}: {e}"

            if got:
                raise RuntimeError(f"LLM 流式中断（已输出部分内容，不再重试）：{last}")
            log.warning("LLM 流式第 %d 次失败：%s", attempt, last)
            time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM 流式连续失败：{last}")
