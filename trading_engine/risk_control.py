"""
09:30/每个半点 ☁️/🖥️ 持仓风控 — 冲高回落即时检测 + 做T决策表
云端 09:30 执行一次，本地 Hermes 后续每个半点调用。
纯数据计算，不调用 LLM。
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from trading_engine.common import TZ, now_str, today_str, feishu_send, get_data_file

try:
    import akshare as ak
    HAS_AK = True
except ImportError:
    HAS_AK = False

# ─── 冲高回落检测框架 ──────────────────────────────

WEIGHTS = {
    "drawdown_from_high": 3,     # (最高-现价)/现价 > 3%
    "below_midpoint": 2,         # 现价 < (最高+最低)/2
    "surge_then_fade": 2,        # 涨幅>5%且现价<开盘
    "high_turnover": 1,          # 换手>5%
}

THRESHOLDS = {
    "drawdown_from_high": 0.03,   # 3%
    "surge_then_fade": 0.05,      # 5%
    "high_turnover": 0.05,        # 5%
}

DECISIONS = {
    (5, 99): ("🔴 立即卖出", "sell"),
    (3, 4): ("🟡 减仓一半", "reduce"),
    (0, 2): ("🟢 正常持有", "hold"),
}


def detect_pullback(stock: dict) -> dict:
    """单只股票冲高回落检测。
    stock 需包含: name, code, current, open, high, low, turnover_rate, prev_close
    返回 {score, decision, reasons, ...}
    """
    score = 0
    reasons = []

    current = stock.get("current", 0)
    high = stock.get("high", 0)
    low = stock.get("low", 0)
    open_price = stock.get("open", 0)
    turnover = stock.get("turnover_rate", 0)
    prev_close = stock.get("prev_close", 1)

    if current <= 0:
        return {"score": 0, "decision": "no_data", "reasons": ["无实时数据"]}

    pct_change = (current - prev_close) / prev_close if prev_close else 0

    # ❶ 从最高点回落超过阈值
    if high > 0:
        drawdown = (high - current) / current
        if drawdown > THRESHOLDS["drawdown_from_high"]:
            score += WEIGHTS["drawdown_from_high"]
            reasons.append(f"从最高 {high:.2f} 回落 {drawdown*100:.1f}%")

    # ❷ 现价低于中点
    if high > 0 and low > 0:
        midpoint = (high + low) / 2
        if current < midpoint:
            score += WEIGHTS["below_midpoint"]
            reasons.append(f"现价 {current:.2f} 低于中点 {midpoint:.2f}")

    # ❸ 大涨后落入开盘价下方
    if pct_change > THRESHOLDS["surge_then_fade"] and current < open_price:
        score += WEIGHTS["surge_then_fade"]
        reasons.append(f"涨幅 {pct_change*100:.1f}% 但已跌破开盘价 {open_price:.2f}")

    # ❹ 换手率过高
    if turnover > THRESHOLDS["high_turnover"]:
        score += WEIGHTS["high_turnover"]
        reasons.append(f"换手率 {turnover*100:.1f}% 偏高")

    # 判定
    decision_str, decision_action = "🟢 正常持有", "hold"
    for (lo, hi), (d, a) in DECISIONS.items():
        if lo <= score <= hi:
            decision_str, decision_action = d, a
            break

    return {
        "name": stock.get("name", "?"),
        "code": stock.get("code", "?"),
        "score": score,
        "decision": decision_action,
        "decision_str": decision_str,
        "reasons": reasons,
        "metrics": {
            "current": current,
            "open": open_price,
            "high": high,
            "low": low,
            "pct_change": round(pct_change * 100, 2),
            "turnover": round(turnover * 100, 2),
            "drawdown_from_high": round((high - current) / current * 100, 2) if high > 0 else 0,
        }
    }


def fetch_position_prices(codes: List[str]) -> List[dict]:
    """获取持仓股的实时数据（单次全市场拉取）"""
    results = []
    if not HAS_AK or not codes:
        return results

    code_set = set(codes)
    try:
        df = ak.stock_zh_a_spot_em()
        for _, r in df.iterrows():
            code = str(r["代码"])
            if code not in code_set:
                continue
            results.append({
                "code": code,
                "name": r["名称"],
                "current": float(r["最新价"]),
                "open": float(r["今开"]),
                "high": float(r["最高"]),
                "low": float(r["最低"]),
                "turnover_rate": float(r.get("换手率", 0)) / 100.0,
                "prev_close": float(r["昨收"]),
            })
    except Exception as e:
        print(f"  ⚠️ 行情拉取失败: {e}")
    return results


def build_trade_decision(stock: dict, pullback: dict) -> str:
    """构造做T决策表（卖100/200/不卖 + 精准盈亏）"""
    if pullback["decision"] == "hold":
        return ""

    current = pullback["metrics"]["current"]
    cost = stock.get("cost", current)
    shares = stock.get("shares", 100)
    pnl = (current - cost) * shares

    lines = [f"📊 {pullback['name']}({pullback['code']})"]
    lines.append(f"   现价: {current:.2f} | 成本: {cost:.2f} | 浮盈: {pnl:+.0f}元")
    lines.append(f"   得分: {pullback['score']}分 → {pullback['decision_str']}")

    for r in pullback['reasons']:
        lines.append(f"   • {r}")

    if pullback["decision"] == "sell":
        lines.append(f"   🎯 卖出全部 {shares}股 锁利 {pnl:+.0f}元")
    elif pullback["decision"] == "reduce":
        half = shares // 2
        lines.append(f"   🎯 减仓 {half}股 锁定 {(current-cost)*half:+.0f}元")

    return "\n".join(lines)


def run(positions: list = None) -> dict:
    """主入口 — 遍历持仓执行风控检测。
    
    positions: 可选，持仓列表 [{code, name, cost, shares}, ...]。
              不传则从 holdings.md 读取（本地模式）。
    """
    print(f"[{now_str()}] 🛡️ 持仓风控引擎启动...")

    # 加载持仓
    if positions is None:
        positions = _load_holdings_local()

    if not positions:
        print("  📭 空仓，无需风控")
        return {"pushed": False, "results": [], "summary": "空仓"}

    codes = [p["code"] for p in positions]
    print(f"  检查 {len(codes)} 只持仓: {codes}")

    # 获取实时价格
    realtime = fetch_position_prices(codes)
    print(f"  获取到 {len(realtime)} 只实时数据")

    # 逐只检测
    alerts = []
    all_clear = True
    lines = []

    for stock in positions:
        rt = next((r for r in realtime if r["code"] == stock.get("code")), None)
        if not rt:
            print(f"  ⚠️ {stock.get('code')}: 无实时数据，跳过")
            continue

        result = detect_pullback(rt)
        decision = build_trade_decision(stock, result)
        alerts.append(result)

        if result["decision"] in ("sell", "reduce"):
            all_clear = False
            if decision:
                lines.append(decision)

    # 构造推送
    if all_clear:
        msg = "✅ 所有持仓正常，无风控预警"
    else:
        msg = "\n\n".join(lines)

    title = f"🛡️ 持仓风控 | {now_str('%H:%M')}"
    body = msg + "\n\n─" * 20 + "\n"
    body += f"⏰ 风控引擎 {now_str('%H:%M')} | "
    body += f"检测 {len(codes)} 只 | 预警 {sum(1 for a in alerts if a['decision'] != 'hold')} 只"

    pushed = feishu_send(title, body).get("success", False)

    # 如果有卖出/减仓信号，单独再发一条醒目告警
    if not all_clear:
        for a in alerts:
            if a["decision"] == "sell":
                feishu_send(
                    f"🔴 冲高回落卖出信号",
                    f"{a['name']}({a['code']})\n"
                    f"得分: {a['score']}分\n"
                    f"原因: {', '.join(a['reasons'])}\n"
                    f"现价: {a['metrics']['current']:.2f} | 回落: {a['metrics']['drawdown_from_high']}%"
                )

    print(f"  📤 推送: {'✅' if pushed else '❌'} | 预警: {sum(1 for a in alerts if a['decision'] != 'hold')}只")
    return {"pushed": pushed, "results": alerts}


def _load_holdings_local() -> list:
    """从 holdings.md 加载持仓（本地+云端通用）"""
    positions = []
    holding_file = get_data_file("holdings.md")
    if not os.path.exists(holding_file):
        return positions

    with open(holding_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("|") and not line.startswith("|--") and not line.startswith("| 代码"):
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 4:
                    positions.append({
                        "code": parts[0],
                        "name": parts[1],
                        "cost": float(parts[2]),
                        "shares": int(parts[3]),
                    })
    return positions


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))