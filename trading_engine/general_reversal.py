"""
10:00 ☁️/🖥️ 通用反转扫描 — 实时资金流版
☁️ 云端: stock_fund_flow_individual(即时) — 同花顺实时资金流 (单次全市场拉取)
🖥️ 本地: 同上，Alpha Lab 用 stock CLI 做补充验证

核心改动: 用 stock_fund_flow_individual(symbol='即时') 替代 stock_individual_fund_flow()
- ✅ 实时盘中数据 (同花顺源，非 T+1)
- ✅ 一次拉取全市场 5000+ 只股票 (非逐只调用)
- ✅ 包含「净额」字段 = 主力净流入
"""

import json
import os
import sys
from datetime import datetime

from trading_engine.common import TZ, now_str, today_str, feishu_send

try:
    import akshare as ak
    import pandas as pd
    HAS_AK = True
except ImportError:
    HAS_AK = False

# ─── 打分规则 ──────────────────────────────────────
SCORING = {
    "major_inflow": 3,      # 主力净流入 > 2000万
    "turnover": 2,          # 换手率 3%-15%
    "j_value": 2,           # J 值 < 30（超卖反转）
    "pct_range": 2,         # 涨幅 2%-8%
    "volatility": 1,        # 近5日振幅 > 5%
}

PENALTIES = {
    "pullback": -2,         # 冲高回落（最高价-现价 > 5%）
    "high_pe": -1,          # PE > 100
    "low_volume": -1,       # 量比 < 1
    "chiNext": -99,         # 创业板（无权限）
}

MIN_SCORE = 6               # 推荐阈值
MAX_PRICE = 25.65           # 价格上限
TOP_N = 500                 # 取资金流前N只活跃股


def check_eligibility(code: str, name: str, price: float) -> bool:
    """基础准入检查"""
    if price <= 0 or price > MAX_PRICE:
        return False
    if code.startswith("300") or code.startswith("301"):
        return False
    if "ST" in name or "*ST" in name:
        return False
    return True


def score_stock(stock: dict) -> dict:
    """对单只股票打分"""
    score = 0
    reasons = []
    penalties = []
    metrics = {}

    pct_change = stock.get("pct_change", 0)
    turnover = stock.get("turnover_rate", 0)
    inflow = stock.get("net_flow", 0)          # 资金净额（万元）
    current = stock.get("current", 0)
    high = stock.get("high", 0)
    volume_ratio = stock.get("volume_ratio", 1.0)
    pe = stock.get("pe", 30)
    amplitude = stock.get("amplitude", 0)

    # ➕ 主力大幅流入（核心维度！）
    if inflow > 2000:
        score += SCORING["major_inflow"]
        reasons.append(f"主力净流入 {inflow:.0f}万")
    elif inflow > 1000:
        score += 1
        reasons.append(f"主力净流入 {inflow:.0f}万")

    # ➕ 换手率适中
    if 0.03 < turnover <= 0.15:
        score += SCORING["turnover"]
        reasons.append(f"换手率 {turnover*100:.1f}%")
    elif 0.02 < turnover <= 0.03:
        score += 1
        reasons.append(f"换手率 {turnover*100:.1f}%")

    # ➕ 涨幅适中
    if 0.02 <= pct_change <= 0.08:
        score += SCORING["pct_range"]
        reasons.append(f"涨幅 {pct_change*100:.1f}%")
    elif 0.01 <= pct_change < 0.02:
        score += 1
        reasons.append(f"涨幅 {pct_change*100:.1f}%")

    # ➕ 振幅
    if amplitude > 0.05:
        score += SCORING["volatility"]
        reasons.append(f"振幅 {amplitude*100:.1f}%")

    # ➕ J值（需要KDJ数据）
    j = stock.get("j_value")
    if j is not None and j < 30:
        score += SCORING["j_value"]
        reasons.append(f"J值={j:.0f}（超卖区）")
    elif j is not None and j < 50:
        score += 1
        reasons.append(f"J值={j:.0f}")

    # ➖ 冲高回落
    if high > 0 and current > 0:
        drawdown = (high - current) / current
        if drawdown > 0.05:
            score += PENALTIES["pullback"]
            penalties.append(f"冲高回落 {drawdown*100:.1f}%")

    # ➖ 高PE
    if pe > 100:
        score += PENALTIES["high_pe"]
        penalties.append(f"PE={pe:.0f}")

    # ➖ 量比不足
    if volume_ratio < 1:
        score += PENALTIES["low_volume"]
        penalties.append(f"量比={volume_ratio:.2f}")

    metrics.update({
        "price": current,
        "pct_change": round(pct_change * 100, 2),
        "turnover_pct": round(turnover * 100, 2),
        "volume_ratio": volume_ratio,
        "pe": pe,
    })

    return {
        "score": score,
        "reasons": reasons,
        "penalties": penalties,
        "metrics": metrics,
        "eligible": score >= MIN_SCORE,
    }


def scan_candidates() -> list:
    """扫描 A 股全市场 — 用 stock_fund_flow_individual(即时) 获取实时资金流"""
    candidates = []
    if not HAS_AK:
        return candidates

    print("  🔍 拉取全市场实时资金流 (同花顺即时数据)...")
    try:
        # ⭐ 核心：同花顺即时资金流，一次拉取全市场
        df_fund = ak.stock_fund_flow_individual(symbol="即时")
        print(f"    资金流: {len(df_fund)} 只")
    except Exception as e:
        print(f"  ❌ 资金流拉取失败: {e}")
        return candidates

    print("  📊 拉取全市场实时行情 (东方财富)...")
    try:
        df_spot = ak.stock_zh_a_spot_em()
        print(f"    行情: {len(df_spot)} 只")
    except Exception as e:
        print(f"  ❌ 行情拉取失败: {e}")
        return candidates

    # ─── 合并数据 ───
    # stock_fund_flow_individual 列: 股票代码, 股票简称, 最新价, 涨跌幅, 换手率, 流入资金, 流出资金, 净额, 成交额
    # stock_zh_a_spot_em 列: 代码, 名称, 最新价, 涨跌幅, 成交量, 成交额, 振幅, 换手率, 量比, 今开, 最高, 最低, 昨收, 市盈率-动态

    # 标准化列名用于合并
    df_fund = df_fund.rename(columns={
        "股票代码": "code",
        "股票简称": "name",
        "最新价": "price_fund",
        "涨跌幅": "pct_fund",
        "换手率": "turnover_fund",
        "净额": "net_flow",      # 万元单位
        "流入资金": "inflow",
        "流出资金": "outflow",
    })
    df_fund["code"] = df_fund["code"].astype(str)

    df_spot = df_spot.rename(columns={
        "代码": "code",
        "名称": "name_spot",
        "最新价": "price",
        "涨跌幅": "pct_spot",
        "振幅": "amplitude",
        "换手率": "turnover_spot",
        "量比": "volume_ratio",
        "今开": "open",
        "最高": "high",
        "最低": "low",
        "昨收": "prev_close",
        "市盈率-动态": "pe",
        "成交额": "amount",
    })
    df_spot["code"] = df_spot["code"].astype(str)

    # 合并：fund flow + spot price
    merged = pd.merge(df_fund, df_spot, on="code", how="inner")
    print(f"    合并后: {len(merged)} 只")

    # 按资金净额排序，取前N
    if "net_flow" in merged.columns:
        merged = merged.sort_values("net_flow", ascending=False).head(TOP_N)

    for _, row in merged.iterrows():
        code = str(row["code"])
        name = str(row.get("name", row.get("name_spot", "")))
        price = float(row.get("price", 0))
        pct_str = str(row.get("pct_spot", row.get("pct_fund", "0"))).replace("%", "")

        try:
            pct_change = float(pct_str) / 100.0
        except (ValueError, TypeError):
            pct_change = 0.0

        turnover_str = str(row.get("turnover_spot", row.get("turnover_fund", "0"))).replace("%", "")
        try:
            turnover_rate = float(turnover_str) / 100.0
        except (ValueError, TypeError):
            turnover_rate = 0.0

        try:
            net_flow = float(row.get("net_flow", 0))
        except (ValueError, TypeError):
            net_flow = 0

        try:
            amplitude = float(row.get("amplitude", 0)) / 100.0
        except (ValueError, TypeError):
            amplitude = 0

        if not check_eligibility(code, name, price):
            continue

        stock = {
            "code": code,
            "name": name,
            "current": price,
            "high": float(row.get("high", price)),
            "pct_change": pct_change,
            "turnover_rate": turnover_rate,
            "volume_ratio": float(row.get("volume_ratio", 1.0)),
            "pe": float(row.get("pe", 30) or 30),
            "net_flow": net_flow,
            "amplitude": amplitude,
            "j_value": None,  # 云端不计算 KDJ，本地 Alpha Lab 补充
        }

        result = score_stock(stock)
        if result["eligible"]:
            candidates.append({
                **stock,
                **result,
                "name": name,
                "code": code,
                "net_flow_display": round(net_flow, 0),
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def run() -> dict:
    """主入口 — 通用反转扫描并推送"""
    print(f"[{now_str()}] 🔄 通用反转引擎启动...")

    candidates = scan_candidates()
    print(f"  🎯 候选 {len(candidates)} 只")

    if not candidates:
        msg = "📭 今日无通用反转候选（无 ≥6 分推荐）"
        feishu_send(f"🔄 通用反转 | {now_str('%H:%M')}", msg)
        return {"pushed": True, "candidates": [], "count": 0}

    top = candidates[:5]
    lines = []
    for i, s in enumerate(top, 1):
        status = "🔥" if s["score"] >= 9 else "⭐" if s["score"] >= 7 else "📌"
        lines.append(f"{status} #{i} **{s['name']}**({s['code']}) — {s['score']}分")
        lines.append(f"   现价 {s['metrics']['price']:.2f} | {s['metrics']['pct_change']:+.2f}% | 换手 {s['metrics']['turnover_pct']:.1f}%")
        if s.get("net_flow_display"):
            lines.append(f"   主力净流入 {s['net_flow_display']:.0f}万")
        if s["reasons"]:
            lines.append(f"   ➕ {' | '.join(s['reasons'])}")
        if s["penalties"]:
            lines.append(f"   ➖ {' | '.join(s['penalties'])}")
        lines.append("")

    body = "\n".join(lines)
    body += "─" * 20 + "\n"
    body += f"⏰ 通用反转引擎 {now_str('%H:%M')} | 扫描 {TOP_N} 只 | 候选 {len(candidates)} 只"

    title = f"🔄 通用反转推荐 | {now_str('%m/%d %H:%M')}"
    pushed = feishu_send(title, body).get("success", False)

    print(f"  📤 推送 {'✅' if pushed else '❌'}")
    return {"pushed": pushed, "candidates": top, "count": len(candidates)}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))