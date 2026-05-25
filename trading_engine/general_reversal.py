"""
10:00 ☁️/🖥️ 通用反转扫描 — 云端实时量价版
☁️ 云端 (GitHub Actions): 纯量价扫描，无资金流（stock_individual_fund_flow 是 T+1 数据）
🖥️ 本地 (Hermes Alpha Lab): 用 stock CLI 做完整资金流分析

独立于龙回头管线，每次独立扫描。打分 ≥5 分推荐。
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

# ─── 打分规则（纯量价，无资金流依赖） ──────────────
SCORING = {
    "turnover": 3,          # 换手率 3%-15%（活跃度高）
    "pct_range": 2,         # 涨幅 2%-8%（不冷不热，刚好）
    "volatility": 2,        # 近5日振幅 > 5%（有波动才有机会）
    "volume_surge": 2,      # 量比 > 2（异常放量 = 资金关注）
    "liquidity": 1,         # 成交额 > 5亿（流动性好）
}
MAX_SCORE = sum(SCORING.values())  # 10

PENALTIES = {
    "pullback": -2,         # 冲高回落（最高价-现价 > 5%）
    "high_pe": -1,          # PE > 100
    "low_volume": -1,       # 量比 < 1
    "chiNext": -99,         # 创业板（无权限）
}

MIN_SCORE = 5               # 推荐阈值
MAX_PRICE = 25.65           # 价格上限
TOP_N = 500                 # 按成交额取前N只活跃股


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
    """对单只股票打分（纯量价维度，无资金流）"""
    score = 0
    reasons = []
    penalties = []
    metrics = {}

    pct_change = stock.get("pct_change", 0)
    turnover = stock.get("turnover_rate", 0)
    current = stock.get("current", 0)
    high = stock.get("high", 0)
    volume_ratio = stock.get("volume_ratio", 1.0)
    pe = stock.get("pe", 30)
    amplitude = stock.get("amplitude", 0)
    amount = stock.get("amount", 0)  # 成交额（元）

    # ➕ 换手率适中（最核心的活跃度指标）
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

    # ➕ 振幅（波动=机会）
    if amplitude > 0.05:
        score += SCORING["volatility"]
        reasons.append(f"振幅 {amplitude*100:.1f}%")

    # ➕ 量比异常（放量=资金关注）
    if volume_ratio > 2.0:
        score += SCORING["volume_surge"]
        reasons.append(f"量比 {volume_ratio:.1f}x")
    elif volume_ratio > 1.5:
        score += 1
        reasons.append(f"量比 {volume_ratio:.1f}x")

    # ➕ 流动性（成交额大=进出方便）
    if amount > 5e8:  # > 5亿
        score += SCORING["liquidity"]
        reasons.append(f"成交额 {amount/1e8:.1f}亿")

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
    """扫描 A 股全市场 — 单次 stock_zh_a_spot_em 调用，无逐只 API"""
    candidates = []
    if not HAS_AK:
        return candidates

    print("  🔍 扫描全市场量价信号（纯实时数据，无资金流）...")
    try:
        # 单次全市场拉取
        df = ak.stock_zh_a_spot_em()
        print(f"    全市场 {len(df)} 只")

        # 按成交额排序取前 N 只活跃股
        df = df.sort_values("成交额", ascending=False).head(TOP_N)

        for _, row in df.iterrows():
            code = str(row["代码"])
            name = str(row["名称"])

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
                "amplitude": float(row.get("振幅", 0)) / 100.0,
                "amount": float(row.get("成交额", 0)),
            }

            if not check_eligibility(stock):
                continue

            result = score_stock(stock)
            if result["eligible"]:
                candidates.append({**stock, **result, "name": name, "code": code})

        # 按得分降序
        candidates.sort(key=lambda x: x["score"], reverse=True)

    except Exception as e:
        print(f"  ❌ 扫描失败: {e}")

    return candidates


def run(is_cloud: bool = False) -> dict:
    """主入口 — 通用反转扫描并推送。
    
    is_cloud: True = GitHub Actions 环境（量价版，标注"轻量扫描"）
              False = 本地（后续 Alpha Lab 会做资金流补充）
    """
    print(f"[{now_str()}] 🔄 通用反转引擎启动{' (☁️ 云端量价版)' if is_cloud else ''}...")

    candidates = scan_candidates()
    print(f"  🎯 候选 {len(candidates)} 只")

    if not candidates:
        label = "☁️ 云端轻量扫描" if is_cloud else "🔄 通用反转"
        msg = f"📭 今日无通用反转候选（无 ≥{MIN_SCORE} 分推荐）"
        if is_cloud:
            msg += "\n\n⚠️ 云端版仅用量价维度打分，不含资金流向。完整分析请等 10:30 Alpha Lab。"
        feishu_send(f"{label} | {now_str('%H:%M')}", msg)
        return {"pushed": True, "candidates": [], "count": 0}

    # 取前5
    top = candidates[:5]

    lines = []
    for i, s in enumerate(top, 1):
        status = "🔥" if s["score"] >= 8 else "⭐" if s["score"] >= 6 else "📌"
        lines.append(f"{status} #{i} **{s['name']}**({s['code']}) — {s['score']}分")
        lines.append(f"   现价 {s['metrics']['price']:.2f} | {s['metrics']['pct_change']:+.2f}% | 换手 {s['metrics']['turnover_pct']:.1f}%")
        if s.get("amount"):
            lines.append(f"   成交额 {s['amount']/1e8:.1f}亿 | 量比 {s['metrics']['volume_ratio']:.1f}")
        if s["reasons"]:
            lines.append(f"   ➕ {' | '.join(s['reasons'])}")
        if s["penalties"]:
            lines.append(f"   ➖ {' | '.join(s['penalties'])}")
        lines.append("")

    body = "\n".join(lines)
    body += "─" * 20 + "\n"
    body += f"⏰ 通用反转引擎 {now_str('%H:%M')} | 扫描 {TOP_N} 只 | 候选 {len(candidates)} 只"
    if is_cloud:
        body += "\n☁️ 云端轻量版 — 纯量价维度，不含资金流向"

    label = "☁️ 通用反转(云端量价)" if is_cloud else "🔄 通用反转推荐"
    title = f"{label} | {now_str('%m/%d %H:%M')}"
    pushed = feishu_send(title, body).get("success", False)

    print(f"  📤 推送 {'✅' if pushed else '❌'}")
    return {"pushed": pushed, "candidates": top, "count": len(candidates)}


if __name__ == "__main__":
    result = run(is_cloud=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))