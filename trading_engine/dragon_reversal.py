"""
龙回头管线 — 09:35 基准快照 + 10:15 执行指令 + 14:30 尾盘扫描
🐉 前3-5日连续流出→转正>2000万 → Day 2 分歧低吸
"""

import json
import os
import sys
from datetime import datetime, timedelta

from trading_engine.common import TZ, now_str, today_str, feishu_send, get_data_file

try:
    import akshare as ak
    HAS_AK = True
except ImportError:
    HAS_AK = False

# ─── 龙回头追踪文件路径 ──────────────────────────────
TRACKER_FILE = get_data_file("dragon_tracker.md")
SNAPSHOT_FILE = get_data_file("dragon_snapshot.json")

MAX_PRICE = 25.65
MODE_SNAPSHOT = "snapshot"    # 09:35 静默基准
MODE_EXECUTE = "execute"      # 10:15 执行指令
MODE_CLOSE = "close"          # 14:30 尾盘


def load_tracker() -> list:
    """从 dragon_tracker.md 加载待观察的 Day 2 股票"""
    stocks = []
    if not os.path.exists(TRACKER_FILE):
        return stocks

    with open(TRACKER_FILE) as f:
        in_table = False
        for line in f:
            line = line.strip()
            if line.startswith("|--"):
                continue
            if line.startswith("| 追踪") or line.startswith("| 代码"):
                in_table = True
                continue
            if in_table and line.startswith("|"):
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 2:
                    code = parts[0]
                    status = parts[-1] if len(parts) > 2 else "⏳"
                    stocks.append({
                        "code": code,
                        "status": status,
                        "raw": parts,
                    })

    return stocks


def fetch_dragon_prices(codes: list) -> list:
    """获取龙回头候选的实时 + 资金流向数据"""
    results = []
    if not HAS_AK or not codes:
        return results

    try:
        df_all = ak.stock_zh_a_spot_em()
    except Exception:
        return results

    for code in codes:
        try:
            row = df_all[df_all["代码"] == code]
            if row.empty:
                continue

            r = row.iloc[0]
            name = r["名称"]
            price = float(r["最新价"])
            pct = float(r["涨跌幅"])
            turnover = float(r.get("换手率", 0))
            volume_ratio = float(r.get("量比", 1.0))
            high = float(r["最高"])
            low = float(r["最低"])
            open_p = float(r["今开"])
            prev = float(r["昨收"])
            amplitude = float(r.get("振幅", 0))

            if price > MAX_PRICE:
                continue

            # 获取资金流向
            main_inflow = 0
            try:
                flow_df = ak.stock_individual_fund_flow(
                    stock=code,
                    market="sh" if code.startswith(("6", "9")) else "sz"
                )
                if flow_df is not None and not flow_df.empty:
                    latest = flow_df.iloc[-1]
                    main_inflow = float(latest.get("主力净流入-净额", 0)) / 10000
            except Exception:
                pass

            # 判断分歧度（价跌量缩/流入温和 = 好信号）
            divergence = "neutral"
            if pct < -3 and main_inflow > 500:
                divergence = "buy_dip"       # 深跌 + 主力仍在进 = 最佳低吸
            elif pct < -1 and main_inflow > 0:
                divergence = "weak_buy"      # 小跌 + 主力微进
            elif pct > 3 and main_inflow < -1000:
                divergence = "sell_surge"    # 大涨 + 主力出 = 危险
            elif pct < -5:
                divergence = "abandon"       # 暴跌放弃
            elif main_inflow > 3000:
                divergence = "strong"        # 强势回封

            results.append({
                "code": code,
                "name": name,
                "price": price,
                "pct": round(pct, 2),
                "turnover": round(turnover, 2),
                "volume_ratio": volume_ratio,
                "high": high,
                "low": low,
                "open": open_p,
                "prev": prev,
                "amplitude": amplitude,
                "main_inflow": round(main_inflow, 2),
                "divergence": divergence,
            })

        except Exception as e:
            print(f"  ⚠️ {code}: {e}")

    return results


def analyze_snapshot(stocks: list) -> list:
    """09:35 基准快照 — 只记录不推送"""
    snapshots = []
    for s in stocks:
        snaps = {
            "code": s["code"],
            "name": s["name"],
            "price": s["price"],
            "pct": s["pct"],
            "main_inflow": s["main_inflow"],
            "time": now_str(),
        }
        snapshots.append(snaps)

    # 写 JSON
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snapshots, f, ensure_ascii=False, indent=2)

    return snapshots


def analyze_execute(stocks: list) -> list:
    """10:15 执行指令 — 分歧低吸判断"""
    instructions = []

    for s in stocks:
        div = s["divergence"]
        instr = {
            "code": s["code"],
            "name": s["name"],
            "price": s["price"],
            "pct": s["pct"],
            "main_inflow": s["main_inflow"],
            "divergence": div,
            "action": "等待",
            "reason": "",
        }

        if div == "buy_dip":
            # 最佳低吸：深跌 + 主力仍在进
            support = round(s["price"] * 0.97, 2)  # 支撑位：现价 -3%
            instr["action"] = "🎯 买入"
            instr["reason"] = (
                f"分歧低吸信号：跌 {s['pct']}% 但主力仍流入 {s['main_inflow']}万\n"
                f"支撑位: {support} 元 | 止损: {round(support * 0.95, 2)} 元\n"
                f"买入价: {s['price']} | 仓位: 100股({s['price']*100:.0f}元)"
            )
        elif div == "weak_buy":
            # 条件稍弱，提示关注
            instr["action"] = "👀 关注"
            instr["reason"] = f"小跌 {s['pct']}% + 主力微进 {s['main_inflow']}万，等待更大跌幅或尾盘确认"
        elif div == "sell_surge":
            instr["action"] = "🚫 放弃"
            instr["reason"] = f"大涨 {s['pct']}% 但主力流出 {s['main_inflow']}万，诱多嫌疑"
        elif div == "abandon":
            instr["action"] = "💀 放弃"
            instr["reason"] = f"暴跌 {s['pct']}%，逻辑失效"
        elif div == "strong":
            instr["action"] = "🔥 强势"
            instr["reason"] = f"主力强势回封 {s['main_inflow']}万，可轻仓追涨"
        else:
            instr["action"] = "⏳ 等待"
            instr["reason"] = f"信号不明确，换手 {s['turnover']}%，量比 {s['volume_ratio']}"

        instructions.append(instr)

    return instructions


def run(mode: str = MODE_EXECUTE) -> dict:
    """主入口。
    mode: 'snapshot' (09:35静默), 'execute' (10:15指令), 'close' (14:30尾盘)
    """
    print(f"[{now_str()}] 🐉 龙回头引擎启动 (mode={mode})...")

    # 读取追踪文件
    pending = [s for s in load_tracker() if "⏳" in s["status"] or "待观察" in s["status"]]
    print(f"  📋 追踪池: {len(pending)} 只待观察")

    if not pending:
        print("  📭 无待观察标的")
        return {"mode": mode, "pushed": False, "count": 0, "results": []}

    codes = [p["code"] for p in pending]
    prices = fetch_dragon_prices(codes)
    print(f"  💹 获取 {len(prices)} 只实时数据")

    if mode == MODE_SNAPSHOT:
        # 静默快照，不推送
        snapshots = analyze_snapshot(prices)
        print(f"  📸 快照 {len(snapshots)} 只 → {SNAPSHOT_FILE}")
        return {"mode": mode, "pushed": False, "count": len(snapshots), "results": snapshots}

    elif mode == MODE_EXECUTE:
        instructions = analyze_execute(prices)
        buy_signals = [i for i in instructions if "买入" in i["action"]]
        watch_signals = [i for i in instructions if "关注" in i["action"]]

        # 构造推送
        lines = []
        for inst in instructions:
            lines.append(f"{inst['action']} **{inst['name']}**({inst['code']}) {inst['price']:.2f}元")
            lines.append(f"   {inst['reason']}")
            lines.append("")

        body = "\n".join(lines) if lines else "📭 今日无明确的龙回头执行信号"
        body += "─" * 20 + "\n"
        body += f"⏰ 龙回头执行引擎 {now_str('%H:%M')} | "
        body += f"追踪 {len(pending)} 只 | 买入 {len(buy_signals)} | 关注 {len(watch_signals)}"

        title = f"🐉 龙回头执行指令 | {now_str('%m/%d %H:%M')}"
        result = feishu_send(title, body)
        pushed = result.get("success", False)

        # 有买入信号时额外推送
        for inst in buy_signals:
            feishu_send(
                "🎯 龙回头买入信号",
                f"{inst['name']}({inst['code']})\n{inst['reason']}"
            )

        print(f"  📤 推送 {'✅' if pushed else '❌'} | 买入 {len(buy_signals)} | 关注 {len(watch_signals)}")
        return {"mode": mode, "pushed": pushed, "count": len(instructions),
                "buy": len(buy_signals), "watch": len(watch_signals), "results": instructions}

    elif mode == MODE_CLOSE:
        # 14:30 尾盘扫描 — 第二次机会
        # 加载上午快照做对比
        snapshot_data = {}
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE) as f:
                for snap in json.load(f):
                    snapshot_data[snap["code"]] = snap

        instructions = []
        for s in prices:
            snap = snapshot_data.get(s["code"], {})
            snap_price = snap.get("price", s["price"])
            snap_inflow = snap.get("main_inflow", s["main_inflow"])
            price_delta = s["price"] - snap_price
            inflow_delta = s["main_inflow"] - snap_inflow

            # 尾盘判断：价格微跌 + 资金改善 = 尾盘低吸
            action = "⏳ 观望"
            reason = f"全天 {s['pct']:+.2f}% | 主力 {s['main_inflow']}万"

            if s["pct"] < -2 and s["main_inflow"] > 500:
                action = "🎯 尾盘买入"
                reason += f"\n   分歧尾盘：跌 {s['pct']}% 但主力仍进 {s['main_inflow']}万"
            elif inflow_delta > 1000 and s["pct"] < 1:
                action = "👀 尾盘关注"
                reason += f"\n   资金改善：较上午 +{inflow_delta}万"

            instructions.append({
                "code": s["code"],
                "name": s["name"],
                "action": action,
                "reason": reason,
                "price": s["price"],
            })

        lines = []
        for inst in instructions:
            lines.append(f"{inst['action']} **{inst['name']}**({inst['code']}) {inst['price']:.2f}元")
            lines.append(f"   {inst['reason']}")
            lines.append("")

        body = "\n".join(lines) if lines else "📭 尾盘无龙回头机会"
        body += "─" * 20 + "\n"
        body += f"⏰ 龙回头尾盘 {now_str('%H:%M')}"

        title = f"🐉 龙回头尾盘 | {now_str('%m/%d %H:%M')}"
        result = feishu_send(title, body)

        print(f"  📤 推送 {'✅' if result.get('success') else '❌'}")
        return {"mode": mode, "pushed": result.get("success", False),
                "count": len(instructions), "results": instructions}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["snapshot", "execute", "close"],
                        default="execute")
    args = parser.parse_args()

    result = run(args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))