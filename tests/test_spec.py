# -*- coding: utf-8 -*-
"""指标口径与文本工具的回归测试。

重点保护 ``norm_label`` 的**序号剥离**：剥多了会把「1年以内」这类账龄行
的前导数字吃掉（那是真实存在的附注行），剥少了则报表行永远匹配不上。
两头都会造成静默错误，所以固化成用例。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # tests/ 的上一级 = 项目根
sys.path.insert(0, str(ROOT))

from shrag_rag.spec import (CORE_METRICS, METRICS, is_statement_table,  # noqa: E402
                            match_metric, norm_label, parse_number)

# (输入标签, 期望的规范化结果)
NORM_CASES = [
    # --- 应剥掉序号 ---
    ("三、利润总额", "利润总额"),
    ("一、营业收入", "营业收入"),
    ("六、利润总额", "利润总额"),
    ("（一）营业收入", "营业收入"),
    ("(一) 营业收入", "营业收入"),
    ("1、货币资金", "货币资金"),
    ("1. 货币资金", "货币资金"),
    ("①货币资金", "货币资金"),
    # --- 应剥掉「其中/减/加」前缀 ---
    ("其中：营业收入", "营业收入"),
    ("减：库存股", "库存股"),
    ("加：其他收益", "其他收益"),
    # --- 原样保留 ---
    ("利润总额", "利润总额"),
    ("归属于上市公司股东的净利润", "归属于上市公司股东的净利润"),
    # --- 关键反例：阿拉伯数字**不带分隔符**时不得剥（否则吃掉账龄行的数字）---
    ("1年以内", "1年以内"),
    ("1-2年", "1-2年"),
    ("2年以上", "2年以上"),
    ("3年以上", "3年以上"),
]

# (输入, 期望数值或 None)
NUM_CASES = [
    ("1,234.56", 1234.56),
    ("（1,234）", -1234.0),
    ("(1,234)", -1234.0),
    ("-39.07", -39.07),
    ("", None),
    ("元", None),
    ("减少0.95个百分点", None),
    ("√适用", None),
    ("-", None),
    ("七、1", None),
]

# 行标签 → 规范化指标名
MATCH_CASES = [
    ("三、利润总额", "利润总额"),
    ("一、营业收入", "营业收入"),
    ("其中：营业收入", "营业收入"),
    ("归属于上市公司股东的净利润", "归母净利润"),
    ("归属于母公司股东的净利润", "归母净利润"),
    ("基本每股收益（元／股）", "基本每股收益"),
    ("资产总计", "总资产"),
    ("总资产", "总资产"),
    # 比率行不得被当成净利润金额（前缀匹配的经典陷阱）
    ("净利润率", None),
    ("毛利率", None),
]


def main() -> int:
    fails: list[str] = []

    print("=" * 88)
    print("norm_label（序号剥离）")
    print("=" * 88)
    for src, want in NORM_CASES:
        got = norm_label(src)
        ok = got == want
        if not ok:
            fails.append(f"norm_label({src!r}) = {got!r}，期望 {want!r}")
        print(f"  {'✅' if ok else '❌'} {src!r:<34} -> {got!r}")

    print("\n" + "=" * 88)
    print("parse_number")
    print("=" * 88)
    for src, want in NUM_CASES:
        got = parse_number(src)
        ok = (got == want) if want is not None else (got is None)
        if not ok:
            fails.append(f"parse_number({src!r}) = {got!r}，期望 {want!r}")
        print(f"  {'✅' if ok else '❌'} {src!r:<24} -> {got!r}")

    print("\n" + "=" * 88)
    print("match_metric")
    print("=" * 88)
    for src, want in MATCH_CASES:
        got = match_metric(src)
        ok = got == want
        if not ok:
            fails.append(f"match_metric({src!r}) = {got!r}，期望 {want!r}")
        print(f"  {'✅' if ok else '❌'} {src!r:<34} -> {got!r}")

    print("\n" + "=" * 88)
    print("口径定义自检")
    print("=" * 88)
    # 归母净利润 与 净利润 必须是两个不同指标，且别名不重叠
    checks = [
        ("归母净利润 与 净利润 是两个独立指标",
         "归母净利润" in METRICS and "净利润" in METRICS
         and METRICS["归母净利润"] != METRICS["净利润"]),
        ("净利润 的别名里不得出现「归属于」字样",
         not any("归属于" in a for a in METRICS["净利润"])),
        ("CORE_METRICS 都是已定义的指标",
         all(m in METRICS for m in CORE_METRICS)),
        ("指标名无重复", len(METRICS) == len(set(METRICS))),
    ]
    for desc, ok in checks:
        if not ok:
            fails.append(desc)
        print(f"  {'✅' if ok else '❌'} {desc}")

    # 报表特征行判定
    bs = [["项目", "附注", "2025"], ["货币资金", "", "1"], ["应收账款", "", "2"],
          ["存货", "", "3"], ["流动资产合计", "", "4"], ["资产总计", "", "5"]]
    ok = is_statement_table(bs)
    if not ok:
        fails.append("资产负债表样本应被判为报表")
    print(f"  {'✅' if ok else '❌'} 资产负债表样本被判为正式报表")

    note = [["行业", "本期", "上期"], ["农业", "1", "2"]]
    ok = not is_statement_table(note)
    if not ok:
        fails.append("分项小表不应被判为报表")
    print(f"  {'✅' if ok else '❌'} 分项小表未被误判为正式报表")

    print("\n" + "=" * 88)
    if fails:
        print(f"❌ {len(fails)} 个用例失败：")
        for f in fails:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部通过（{len(NORM_CASES) + len(NUM_CASES) + len(MATCH_CASES) + 6} 个断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
