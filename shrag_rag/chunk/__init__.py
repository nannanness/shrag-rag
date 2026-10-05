# -*- coding: utf-8 -*-
"""切片层：把 ParsedDoc 切成带元数据的 chunk。"""

from .chunker import Chunk, chunk_document, iter_chunks

__all__ = ["Chunk", "chunk_document", "iter_chunks"]
