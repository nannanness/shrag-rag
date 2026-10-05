# -*- coding: utf-8 -*-
"""FAISS 向量库：一个总库 + 元数据过滤。

## 为什么是一个总库而不是一家一个

- **跨公司提问是核心用例**（「对比浦发和招商银行的资产质量」），
  一家一个库就得扇出到多个库再归并
- 规模上完全没必要：全库 6 万 × 1024 维 = 234 MB，`IndexFlatIP` 是暴力检索，
  一次查询在毫秒级
- 按公司过滤用 ``IDSelectorBatch`` **预过滤**（只在子集里检索），
  而不是"检索完再挑" —— 后者会出现"取了 10 条结果、过滤后只剩 2 条"

## 两个必须遵守的 FAISS 约束（都是实测踩出来的）

1. **向量必须 L2 归一化**：``IndexFlatIP`` 不归一化时算的是「模长 × 相似度」。
   实测把一个向量同方向放大 10 倍，它的分数变成 7.543（>1）并把真正的
   最优结果（0.759）挤到第二 —— 排序直接失效
2. **索引路径必须是纯 ASCII**：写中文文件名时 faiss **不报错，但会写到乱码名**
   （``_中文.faiss`` → 实际生成 ``_涓枃.faiss``）。这种静默写错最难查。
   写中文**目录**时才会正常报 RuntimeError
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class VectorStore:
    """FAISS 索引 + ``行号 → chunk_id`` 映射。"""

    def __init__(self, dim: int = 1024):
        import faiss
        self.dim = dim
        self.index = faiss.IndexFlatIP(dim)     # 内积；配合归一化 = 余弦
        self.ids: list[str] = []                # 第 i 行对应 FAISS id i
        self._pos: dict[str, int] = {}          # chunk_id -> 行号

    # -- 构建 -------------------------------------------------------------- #

    def add(self, vectors: np.ndarray, ids: list[str]) -> None:
        if vectors.shape[0] != len(ids):
            raise ValueError(f"向量数 {vectors.shape[0]} 与 id 数 {len(ids)} 不符")
        if vectors.shape[1] != self.dim:
            raise ValueError(f"维度 {vectors.shape[1]} != {self.dim}")
        self.index.add(np.ascontiguousarray(vectors, dtype="float32"))
        for cid in ids:
            self._pos[cid] = len(self.ids)
            self.ids.append(cid)

    @property
    def ntotal(self) -> int:
        return self.index.ntotal

    # -- 持久化 ------------------------------------------------------------ #

    def save(self, out_dir: Path, manifest: dict | None = None) -> dict:
        import faiss
        out_dir = Path(out_dir)
        # 注意是 str(...).isascii()，Path 对象没有这个方法
        assert str(out_dir).isascii(), (
            f"索引路径必须纯 ASCII，否则 faiss 会静默写到乱码文件名：{out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)

        faiss.write_index(self.index, str(out_dir / "vectors.faiss"))
        with open(out_dir / "id_map.jsonl", "w", encoding="utf-8") as f:
            for cid in self.ids:
                f.write(cid + "\n")

        meta = {"ntotal": self.ntotal, "dim": self.dim,
                **(manifest or {})}
        (out_dir / "manifest.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        return meta

    @classmethod
    def load(cls, in_dir: Path) -> "VectorStore":
        import faiss
        in_dir = Path(in_dir)
        vs = cls.__new__(cls)
        vs.index = faiss.read_index(str(in_dir / "vectors.faiss"))
        vs.dim = vs.index.d
        vs.ids = [l.strip() for l in
                  (in_dir / "id_map.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        vs._pos = {cid: i for i, cid in enumerate(vs.ids)}
        if vs.index.ntotal != len(vs.ids):
            raise RuntimeError(
                f"索引与 id 映射不一致：{vs.index.ntotal} 向量 vs {len(vs.ids)} 个 id")
        return vs

    # -- 检索 -------------------------------------------------------------- #

    def search(self, qvec: np.ndarray, topk: int = 10,
               allowed: set[str] | None = None) -> list[tuple[str, float]]:
        """检索，返回 ``[(chunk_id, score)]``。

        ``allowed`` 非空时用 ``IDSelectorBatch`` **预过滤** —— 只在指定
        chunk 子集里检索，保证 top-k 全部来自子集。
        """
        import faiss
        q = np.ascontiguousarray(qvec, dtype="float32")
        if q.ndim == 1:
            q = q[None, :]
        faiss.normalize_L2(q)                    # 查询向量同样要归一化

        params = None
        k = topk
        if allowed:
            keep = sorted(self._pos[c] for c in allowed if c in self._pos)
            if not keep:
                return []
            params = faiss.SearchParameters()
            params.sel = faiss.IDSelectorBatch(np.asarray(keep, dtype="int64"))
            k = min(topk, len(keep))

        D, I = self.index.search(q, k, params=params)
        out = []
        for score, i in zip(D[0], I[0]):
            if i < 0:
                continue
            out.append((self.ids[i], float(score)))
        return out

    def search_by_meta(self, qvec: np.ndarray, chunks: dict[str, dict],
                       topk: int = 10, **filters) -> list[tuple[str, float]]:
        """按元数据（如 ``code``、``kind``、``year``）过滤后检索。

        ``chunks`` 为 ``chunk_id -> 元数据`` 的映射（通常来自 chunks.jsonl）。
        """
        if not filters:
            return self.search(qvec, topk)
        allowed = {cid for cid, m in chunks.items()
                   if all(m.get(k) == v for k, v in filters.items())}
        # 需要检索更多候选时，预过滤已经保证结果都来自子集，直接用 topk
        return self.search(qvec, topk, allowed=allowed)
