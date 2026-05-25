"""
10:00 ☁️/🖥️ 通用反转扫描 — 今日主力突然大幅流入 + 打分 ≥6 分推荐
不要求"连续流出→转正"（那是龙回头的逻辑）。
独立于龙回头管线，每次独立扫描。
"""

import json
import os
import sys
from datetime import datetime

from trading_engine.common import TZ, now_str, today_str, feishu_send

try:
    import akshare as ak
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


def check_eligibility(stock: dict) -> bool:
    """基础准入检查"""
    price = stock.get("current", 0)
    code = stock.get("code", "")

    if price <= 0 or price > MAX_PRICE:
        return False

    # 过滤创业板
    if code.startswith("300") or code.startswith("301"):
        return False

    # 过滤ST
    name = stock.get("name", "")
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
    inflow = stock.get("main_inflow", 0)
    current = stock.get("current", 0)
    high = stock.get("high", 0)
    volume_ratio = stock.get("volume_ratio", 1.0)
    pe = stock.get("pe", 30)

    # ➕ 主力大幅流入
    if inflow > 2000:
        score += SCORING["major_inflow"]
        reasons.append(f"主力净流入 {inflow:.0f}万")
        metrics["inflow_pct"] = round(inflow / 10000, 2)
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
    amplitude = stock.get("amplitude", 0)
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
    """扫描 A 股全市场，返回 ≥6 分的候选"""
    candidates = []
    if not HAS_AK:
        return candidates

    print("  🔍 扫描全市场主力流入...")
    try:
        # 用 stock_zh_a_spot_em 获取全量
        df = ak.stock_zh_a_spot_em()
        print(f"    获取 {len(df)} 只")

        # 按成交额排序取前 500（排除无成交的死股）
        df = df.sort_values("成交额", ascending=False).head(500)

        for _, row in df.iterrows():
            code = row["代码"]
            name = row["名称"]

            # 基础过滤
            stock = {
                "code": code,
                "name": name,
                "current": float(row["最新价"]),
                "high": float(row["最高"]),
                "pct_change": float(row["涨跌幅"]) / 100.0,
                "turnover_rate": float(row.get("换手率", 0)) / 100.0,
                "volume_ratio": float(row.get("量比", 1.0)),
                "pe": float(row.get("市盈率-动态", 30) or 30),
                "main_inflow": 0,
                "amplitude": float(row.get("振幅", 0)) / 100.0,
            }

            if not check_eligibility(stock):
                continue

            # 获取资金流向
            try:
                flow_df = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith(("6", "9")) else "sz")
                if flow_df is not None and not flow_df.empty:
                    latest = flow_df.iloc[-1]
                    stock["main_inflow"] = float(latest.get("主力净流入-净额", 0)) / 10000  # 转万元
            except Exception:
                pass

            result = score_stock(stock)
            if result["eligible"]:
                candidates.append({**stock, **result, "name": name, "code": code})

        # 按得分降序
        candidates.sort(key=lambda x: x["score"], reverse=True)

    except Exception as e:
        print(f"  ❌ 扫描失败: {e}")

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

    # 取前5
    top = candidates[:5]

    lines = []
    for i, s in enumerate(top, 1):
        status = "🔥" if s["score"] >= 9 else "⭐" if s["score"] >= 7 else "📌"
        lines.append(f"{status} #{i} **{s['name']}**({s['code']}) — {s['score']}分")
        lines.append(f"   现价 {s['metrics']['price']:.2f} | {s['metrics']['pct_change']:+.2f}% | 换手 {s['metrics']['turnover_pct']:.1f}%")
        if s.get("main_inflow"):
            lines.append(f"   主力净流入 {s['main_inflow']:.0f}万")
        if s["reasons"]:
            lines.append(f"   ➕ {' | '.join(s['reasons'])}")
        if s["penalties"]:
            lines.append(f"   ➖ {' | '.join(s['penalties'])}")
        lines.append("")

    body = "\n".join(lines)
    body += "─" * 20 + "\n"
    body += f"⏰ 通用反转引擎 {now_str('%H:%M')} | 扫描500只 | 候选{len(candidates)}只"

    title = f"🔄 通用反转推荐 | {now_str('%m/%d %H:%M')}"
    pushed = feishu_send(title, body).get("success", False)

    print(f"  📤 推送 {'✅' if pushed else '❌'}")
    return {"pushed": pushed, "candidates": top, "count": len(candidates)}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))