# -*- coding: utf-8 -*-
"""问答层：检索 → 结构化数据注入 → 生成 → 引用渲染。"""

from .llm import DashScopeLLM, DEFAULT_MODEL
from .prompt import SYSTEM_PROMPT, build_messages, render_citation
from .qa import Answer, RagQA

__all__ = ["RagQA", "Answer", "DashScopeLLM", "DEFAULT_MODEL",
           "SYSTEM_PROMPT", "build_messages", "render_citation"]
