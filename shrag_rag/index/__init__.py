# -*- coding: utf-8 -*-
"""索引层：嵌入 + 向量库。

当前只做向量检索；BM25（混合检索的另一半）留到下一步。
"""

from .embedder import (BATCH_SIZE, DIM, MODEL, DashScopeEmbedder,  # noqa: F401
                       embed_chunks_resumable, get_api_key)
from .vectorstore import VectorStore

__all__ = ["DashScopeEmbedder", "embed_chunks_resumable", "get_api_key",
           "VectorStore", "MODEL", "DIM", "BATCH_SIZE"]
