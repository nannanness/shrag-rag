# -*- coding: utf-8 -*-
"""阶段 4：嵌入 + 建本地向量索引。

用法::

    # 试点 10 家（约 1 元 / 2 分钟）
    python scripts/06_index.py --limit 10

    # 全量
    python scripts/06_index.py --limit 0

    # 只重建 FAISS（不调 API）—— 已嵌入的向量缓存在 data/index/embeddings/
    python scripts/06_index.py --rebuild-only

产出::

    data/index/embeddings/<code>.npy      该公司向量（n × 1024 float32，已归一化）
    data/index/embeddings/<code>.jsonl    对应的 chunk_id（与 .npy 行号一一对应）
    data/index/vectors.faiss              全库索引
    data/index/id_map.jsonl               FAISS 行号 → chunk_id
    data/index/manifest.json              构建参数 + 切片文件哈希
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.index import DashScopeEmbedder, VectorStore, embed_chunks_resumable  # noqa: E402
from shrag_rag.index.embedder import (BATCH_SIZE, CHARS_PER_TOKEN,  # noqa: E402
                                      DIM, MODEL, TPM_LIMIT)

CHUNKS = ROOT / "data" / "chunks" / "chunks.jsonl"
INDEX_DIR = ROOT / "data" / "index"
EMB_DIR = INDEX_DIR / "embeddings"
LOGS = ROOT / "logs"


def setup_logging(verbose: bool) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOGS / "index.log", encoding="utf-8")])
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()[:16]


def load_chunks(limit: int, codes: list[str] | None) -> tuple[dict, list[str]]:
    """读 chunks.jsonl，按公司分组。返回 (code -> [chunk], 公司顺序)。"""
    by_code: dict[str, list] = {}
    order: list[str] = []
    with open(CHUNKS, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if codes and c["code"] not in codes:
                continue
            if c["code"] not in by_code:
                by_code[c["code"]] = []
                order.append(c["code"])
            by_code[c["code"]].append(c)
    order.sort()
    if limit:
        order = order[:limit]
        by_code = {c: by_code[c] for c in order}
    return by_code, order


def main() -> int:
    ap = argparse.ArgumentParser(description="嵌入并建本地向量索引")
    ap.add_argument("--limit", type=int, default=10, help="取前 N 家公司（0 = 全部）")
    ap.add_argument("--codes", type=str, default=None, help="指定代码，逗号分隔")
    ap.add_argument("--dim", type=int, default=DIM)
    ap.add_argument("--force", action="store_true", help="重嵌入已有缓存")
    ap.add_argument("--workers", type=int, default=8,
                    help="嵌入并发数（默认 8）。单请求约 4 秒而 TPM 只用掉 5%%，"
                         "所以瓶颈是延迟不是额度，并发是最有效的加速手段")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="只用已缓存的向量重建 FAISS，不调 API")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    if not CHUNKS.exists():
        print(f"找不到 {CHUNKS}，先跑 05_chunk.py")
        return 1

    codes = {c.strip() for c in args.codes.split(",")} if args.codes else None
    by_code, order = load_chunks(args.limit, codes)
    total = sum(len(v) for v in by_code.values())
    logging.info("选定 %d 家公司，共 %d 个 chunk", len(order), total)
    if not total:
        return 1

    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 1. 逐公司嵌入（可断点续跑）------------------------------------- #
    t0 = time.time()
    n_done = 0
    embedder = None
    bundles: list[tuple[str, np.ndarray, list[str]]] = []

    for code in order:
        chunks = by_code[code]
        cached = (EMB_DIR / f"{code}.npy").exists() and not args.force
        if args.rebuild_only:
            arr = np.load(EMB_DIR / f"{code}.npy")
            ids = [l.strip() for l in (EMB_DIR / f"{code}.jsonl")
                   .read_text(encoding="utf-8").splitlines() if l.strip()]
            bundles.append((code, arr, ids))
            continue
        if embedder is None:
            embedder = DashScopeEmbedder(dim=args.dim, workers=args.workers)
        arr, ids = embed_chunks_resumable(embedder, code, chunks, EMB_DIR,
                                          force=args.force)
        bundles.append((code, arr, ids))
        n_done += len(ids)
        el = time.time() - t0
        rate = n_done / el if el else 0
        logging.info("[%2d/%d] %s  %d 条  累计 %d 条  %.0fs  (%.1f 条/秒)",
                     order.index(code) + 1, len(order), code, len(ids),
                     n_done, el, rate)

    if embedder is not None:
        logging.info("嵌入完成：%d 条，%d 次调用，%.2f M tokens，%.1f 分钟",
                     n_done, embedder.total_calls, embedder.total_tokens / 1e6,
                     (time.time() - t0) / 60)
        if embedder.total_tokens:
            print(f"\n  本次实际消耗 {embedder.total_tokens/1e6:.2f} M tokens"
                  f" → 约 {embedder.total_tokens/1e6*0.5:.2f} 元\n")

    # ---- 2. 建 FAISS 索引 ------------------------------------------------ #
    vs = VectorStore(dim=args.dim)
    for code, arr, ids in bundles:
        vs.add(arr, ids)
    logging.info("索引建成：%d 个向量，维度 %d", vs.ntotal, vs.dim)

    manifest = {
        "model": MODEL, "dim": args.dim, "batch_size": BATCH_SIZE,
        "tpm_limit": TPM_LIMIT, "chars_per_token": round(CHARS_PER_TOKEN, 6),
        "companies": order, "n_companies": len(order),
        "chunks_file": "data/chunks/chunks.jsonl",
        "chunks_sha256_16": file_hash(CHUNKS),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    meta = vs.save(INDEX_DIR, manifest)

    # ---- 3. 自检：真的能检索吗 ------------------------------------------- #
    print("\n" + "=" * 76)
    print("索引自检")
    print("=" * 76)
    print(f"  向量数        {vs.ntotal}")
    print(f"  维度          {vs.dim}")
    print(f"  公司数        {len(order)}")
    print(f"  切片哈希      {manifest['chunks_sha256_16']}")
    size_mb = (INDEX_DIR / "vectors.faiss").stat().st_size / 1e6
    print(f"  索引文件      {size_mb:.1f} MB  ({INDEX_DIR / 'vectors.faiss'})")

    # 用一条真实 chunk 的向量做检索，确认返回的是它自己
    sample_code = order[0]
    arr = np.load(EMB_DIR / f"{sample_code}.npy")
    ids = [l.strip() for l in (EMB_DIR / f"{sample_code}.jsonl")
           .read_text(encoding="utf-8").splitlines() if l.strip()]
    hits = vs.search(arr[0], topk=3)
    print(f"\n  自检索（用 {ids[0]} 的向量查自己）：")
    for cid, sc in hits:
        mark = "✅" if cid == ids[0] else "  "
        print(f"    {mark} {sc:.4f}  {cid}")
    ok = hits and hits[0][0] == ids[0]
    print(f"\n  {'✅ 自检通过' if ok else '❌ 自检失败：向量与 id 映射可能错位'}")

    # 过滤检索自检
    allowed = set(ids[:50])
    hits2 = vs.search(arr[0], topk=3, allowed=allowed)
    ok2 = all(cid in allowed for cid, _ in hits2)
    print(f"  {'✅' if ok2 else '❌'} 按 id 子集预过滤检索正常"
          f"（限 {len(allowed)} 条，返回 {[c for c, _ in hits2]}）")

    print(f"\n  索引目录      {INDEX_DIR}")
    return 0 if (ok and ok2) else 2


if __name__ == "__main__":
    raise SystemExit(main())
