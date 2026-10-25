# -*- coding: utf-8 -*-
"""Web 服务：问答（流式）与指标浏览。

启动::

    python -m uvicorn web.app:app --host 127.0.0.1 --port 8000

索引只加载一次（约 1.3 GB 内存），所以**不要开多 worker，也不要 --reload**：
每次重载都要重新读 234 MB 的 faiss 索引。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException                       # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse    # noqa: E402
from fastapi.staticfiles import StaticFiles                      # noqa: E402
from pydantic import BaseModel                                   # noqa: E402

from shrag_rag.answer import DashScopeLLM, RagQA                 # noqa: E402
from shrag_rag.answer.qa import UNIT_MUL                         # noqa: E402
from shrag_rag.retrieve import HybridRetriever                   # noqa: E402
from shrag_rag.spec import METRIC_ORDER                          # noqa: E402

INDEX_DIR = ROOT / "data" / "index"
CHUNKS = ROOT / "data" / "chunks" / "chunks.jsonl"
METRICS = ROOT / "data" / "metrics" / "metrics_long.csv"
STATIC = Path(__file__).resolve().parent / "static"

log = logging.getLogger("web")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                    datefmt="%H:%M:%S")

# 进程内单例。索引 + BM25 加起来 1.3 GB，绝不能每个请求重载。
STATE: dict = {}

DEMO = [
    ("数值", "白云机场2025年的营业收入是多少"),
    ("数值", "浦发银行2025年归属于母公司股东的净利润是多少"),
    ("排序", "哪家净利润最高"),
    ("定性", "皖通高速的主要业务是什么"),
    ("事实", "华能国际2025年年报的审计机构是哪家"),
    ("跨公司", "哪家公司的审计机构是立信会计师事务所"),
]


# 金额类指标：上市公司的量级下界（100 万元），低于它基本可以断定是单位标错
MONEY_METRICS = {"营业收入", "利润总额", "净利润", "归母净利润", "扣非归母净利润",
                 "经营活动现金流量净额", "归母净资产", "总资产"}
# 流量类：这些值不该远超同年的营业收入
FLOW_METRICS = {"营业收入", "利润总额", "净利润", "归母净利润",
                "扣非归母净利润", "经营活动现金流量净额"}
MIN_MONEY = 1e6        # 金额类下界：100 万元
MIN_REVENUE = 1e8      # 营收锚点的可信下界：1 亿元


def _build_metrics_view(qa: RagQA) -> dict[int, dict]:
    """把长表按年份组织成「公司 × 指标」，供浏览页使用。

    每个单元格带上原文单位，同时给一个折算到「元」的 ``vb``：
    页面上显示原文（百万元/元），排序要用 ``vb``，否则不同单位的公司没法比大小。

    还会标出 ``suspect``。抽取层有单位标错的个例 —— 实测北方稀土营业收入被抽成
    ``29.11 元``、保利发展净利润被标成「万元」放大一万倍。两条判据都很保守：

    1. 金额类指标低于 100 万元 —— 上市公司不可能这么小
    2. 流量类指标超过同年营业收入的 3 倍，且营收锚点本身 ≥ 1 亿元

    **不能**用「同列中位数」比量级：银行总资产上十万亿是真实值，
    按中位数一筛就会把招商、浦发全标成异常。
    """
    # 营收锚点，优先标准表来源
    rev: dict[tuple[str, int], tuple[float, str]] = {}
    for (code, metric, year), v in qa.metrics.items():
        if metric != "营业收入":
            continue
        base = v["value"] * UNIT_MUL.get(v.get("unit") or "", 1.0)
        cur = rev.get((code, year))
        if cur is None or (v.get("source") == "summary" and cur[1] != "summary"):
            rev[(code, year)] = (base, v.get("source") or "")

    by_year: dict[int, dict] = {}
    for (code, metric, year), v in qa.metrics.items():
        companies = by_year.setdefault(year, {})
        row = companies.setdefault(code, {"code": code,
                                          "name": v.get("name") or "",
                                          "cells": {}})
        unit = v.get("unit") or ""
        base = v["value"] * UNIT_MUL.get(unit, 1.0)
        suspect = ""
        if metric in MONEY_METRICS and abs(base) < MIN_MONEY:
            suspect = "数值量级异常，建议核对原文"
        elif metric in FLOW_METRICS and metric != "营业收入":
            anchor = rev.get((code, year))
            if (anchor and abs(anchor[0]) >= MIN_REVENUE
                    and abs(base) > 3 * abs(anchor[0])):
                suspect = "与同年营业收入量级差异过大，建议核对原文"
        row["cells"][metric] = {
            "v": v["value"], "unit": unit, "vb": base,
            "src": v.get("source") or "", "page": v.get("page") or "",
            "suspect": suspect,
        }
    return by_year


@asynccontextmanager
async def lifespan(app: FastAPI):
    t0 = time.time()

    def load():
        retr = HybridRetriever(INDEX_DIR, CHUNKS)
        llm = DashScopeLLM()
        qa = RagQA(retr, llm, metrics_path=METRICS)
        return retr, llm, qa

    if not INDEX_DIR.exists():
        raise RuntimeError(f"没有索引：{INDEX_DIR}，先跑 scripts/06_index.py")
    retr, llm, qa = await asyncio.to_thread(load)
    STATE.update(retr=retr, llm=llm, qa=qa,
                 metrics_view=_build_metrics_view(qa),
                 ready_at=time.time() - t0)
    # 放后台：服务立刻可访问，BM25 约 4 秒后自行就绪
    retr.warmup(background=True)
    log.info("就绪：%d 向量 / %d chunk / %d 家 / 指标 %d 条，耗时 %.1fs",
             retr.vs.ntotal, len(retr.chunks), len(retr.companies),
             len(qa.metrics), STATE["ready_at"])
    yield
    STATE.clear()


app = FastAPI(title="shrag-rag", lifespan=lifespan)


class AskIn(BaseModel):
    question: str
    topk: int = 5


@app.get("/api/health")
def health():
    retr = STATE.get("retr")
    if retr is None:
        raise HTTPException(503, "还没加载完")
    return {"ok": True, "vectors": retr.vs.ntotal,
            "chunks": len(retr.chunks), "companies": len(retr.companies),
            "metrics": len(STATE["qa"].metrics),
            "bm25_ready": retr._bm25 is not None,
            "load_seconds": round(STATE["ready_at"], 2)}


@app.get("/api/demo")
def demo():
    return {"items": [{"tag": t, "q": q} for t, q in DEMO]}


@app.get("/api/metrics")
def metrics(year: int = 2025):
    view = STATE["metrics_view"].get(year)
    if not view:
        raise HTTPException(404, f"没有 {year} 年的数据")
    present = set()
    for row in view.values():
        present |= set(row["cells"])
    order = [m for m in METRIC_ORDER if m in present]
    rows = sorted(view.values(), key=lambda r: r["code"])
    return {"year": year, "metrics": order,
            "available_years": sorted(STATE["metrics_view"]),
            "rows": rows}


@app.post("/api/ask")
def ask(body: AskIn):
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    if len(question) > 200:
        raise HTTPException(400, "问题太长")

    qa: RagQA = STATE["qa"]

    def gen():
        t0 = time.time()
        try:
            for event, payload in qa.stream(question, topk=body.topk):
                if event == "done":
                    payload["elapsed_ms"] = int((time.time() - t0) * 1000)
                data = json.dumps(payload, ensure_ascii=False, default=str)
                yield f"event: {event}\ndata: {data}\n\n"
        except Exception as e:                        # noqa: BLE001
            log.exception("问答失败")
            data = json.dumps({"text": f"服务端出错：{type(e).__name__}: {e}",
                               "ok": False}, ensure_ascii=False)
            yield f"event: done\ndata: {data}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
