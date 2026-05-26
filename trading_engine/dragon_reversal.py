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
    """从 dragon_tracker.md 加载活跃追踪股票（仅「活跃追踪」表）"""
    stocks = []
    if not os.path.exists(TRACKER_FILE):
        return stocks

    with open(TRACKER_FILE) as f:
        in_active_table = False
        for line in f:
            line = line.strip()
            # 只解析「活跃追踪」区域，遇到「历史归档」或下一节就停止
            if "📦 历史归档" in line or "## 📊" in line:
                if in_active_table:
                    break
            if "🔴 活跃追踪" in line:
                in_active_table = True
                continue
            if not in_active_table:
                continue
            if line.startswith("|--") or not line.startswith("|"):
                continue
            if "代码" in line and "名称" in line:
                continue
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 2:
                code = parts[0]
                status = parts[-1]
                stocks.append({
                    "code": code,
                    "status": status,
                    "raw": parts,
                })
    return stocks


def fetch_dragon_prices(codes: list) -> list:
    """获取龙回头候选的实时数据（量价 + 资金流）。
    
    数据源:
    - stock_fund_flow_individual(即时): 同花顺实时资金流（全市场一次拉取）
    - stock_zh_a_spot_em(): 东方财富实时行情（量比/高低开等）
    
    2026-05-26: 增加重试逻辑（收盘后API容易超时）
    """
    results = []
    if not HAS_AK or not codes:
        return results

    import time as _time
    code_set = set(codes)

    # ① 拉取全市场实时资金流（同花顺）— 重试最多3次
    df_fund = None
    for attempt in range(3):
        try:
            df_fund = ak.stock_fund_flow_individual(symbol="即时")
            break
        except Exception as e:
            print(f"  ⚠️ 资金流拉取尝试 {attempt+1}/3 失败: {e}")
            if attempt < 2:
                _time.sleep(3)
    if df_fund is None:
        print("  ❌ 资金流3次重试全部失败")
        return results

    df_fund = df_fund.rename(columns={
        "股票代码": "code", "股票简称": "name",
        "最新价": "price_fund", "涨跌幅": "pct_str",
        "换手率": "turnover_str", "净额": "net_flow",
    })
    df_fund["code"] = df_fund["code"].astype(str)

    # ② 拉取全市场行情（东方财富）— 重试最多3次
    df_spot = None
    for attempt in range(3):
        try:
            df_spot = ak.stock_zh_a_spot_em()
            break
        except Exception as e:
            print(f"  ⚠️ 行情拉取尝试 {attempt+1}/3 失败: {e}")
            if attempt < 2:
                _time.sleep(3)
    if df_spot is None:
        print("  ❌ 行情3次重试全部失败")
        return results

    df_spot = df_spot.rename(columns={
        "代码": "code", "最新价": "price",
        "涨跌幅": "pct_spot", "振幅": "amplitude",
        "量比": "volume_ratio", "今开": "open",
        "最高": "high", "最低": "low", "昨收": "prev_close",
        "换手率": "turnover_spot", "市盈率-动态": "pe",
    })
    df_spot["code"] = df_spot["code"].astype(str)

    # ③ 合并 + 筛选追踪池中的股票
    import pandas as pd
    merged = pd.merge(df_fund, df_spot, on="code", how="inner")
    merged = merged[merged["code"].isin(code_set)]

    for _, r in merged.iterrows():
        code = str(r["code"])
        name = str(r.get("name", ""))
        price = float(r.get("price", 0))
        pct_str = str(r.get("pct_str", r.get("pct_spot", "0"))).replace("%", "")
        try:
            pct = float(pct_str)
        except (ValueError, TypeError):
            pct = 0.0

        turnover_str = str(r.get("turnover_str", r.get("turnover_spot", "0"))).replace("%", "")
        try:
            turnover = float(turnover_str)
        except (ValueError, TypeError):
            turnover = 0.0

        try:
            net_flow = float(r.get("net_flow", 0))  # 万元
        except (ValueError, TypeError):
            net_flow = 0.0

        volume_ratio = float(r.get("volume_ratio", 1.0))
        high = float(r.get("high", price))
        low = float(r.get("low", price))
        open_p = float(r.get("open", price))
        prev = float(r.get("prev_close", price))
        amplitude = float(r.get("amplitude", 0))

        if price > MAX_PRICE:
            continue

        # ─── 分歧度判断（量价 + 实时资金流） ───
        divergence = "neutral"

        if pct < -3 and net_flow > 500:
            divergence = "buy_dip"       # 深跌 + 主力仍在进 = 最佳低吸
        elif pct < -1 and net_flow > 0:
            divergence = "weak_buy"      # 小跌 + 主力微进
        elif pct > 3 and net_flow < -1000:
            divergence = "sell_surge"    # 大涨 + 主力流出 = 危险
        elif pct < -5:
            divergence = "abandon"       # 暴跌放弃
        elif net_flow > 3000:
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
            "main_inflow": round(net_flow, 2),
            "divergence": divergence,
        })

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


def analyze_execute(stocks: list) -> dict:
    """10:15 执行指令 — Day 2 完整回访分析。
    
    返回结构化数据用于构造丰富推送。
    """
    # 加载 09:35 快照
    snapshot_data = {}
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            for snap in json.load(f):
                snapshot_data[snap["code"]] = snap

    results = []
    limit_up_count = 0
    
    for s in stocks:
        code = s["code"]
        name = s["name"]
        price = s["price"]
        pct = s["pct"]
        net_flow = s["main_inflow"]
        turnover = s["turnover"]
        vol_ratio = s["volume_ratio"]
        high = s["high"]
        low = s["low"]
        open_p = s["open"]
        prev = s["prev_close"]
        amplitude = s["amplitude"]
        
        snap = snapshot_data.get(code, {})
        snap_inflow = snap.get("main_inflow", net_flow)
        inflow_delta = net_flow - snap_inflow
        
        # ─── 涨停检测 ───
        limit_up_price = round(prev * 1.10, 2)  # 10% 涨停价
        is_limit_up = abs(price - limit_up_price) < 0.02 and pct > 9.0
        if is_limit_up:
            limit_up_count += 1
        
        # ─── 涨停开板检测 ───
        opened_high = high > limit_up_price * 1.005 if is_limit_up else False
        drawdown_from_high = round((high - price) / high * 100, 2) if high > price else 0
        
        # ─── 判定 ───
        action = ""
        action_icon = ""
        analysis = []
        
        if is_limit_up:
            # 涨停中的细分判断
            if turnover < 2.0 and net_flow > 3000:
                action = "🔥 强势封板"
                action_icon = "🔥"
                analysis.append("涨停封死+缩量锁仓，筹码稳定")
            elif turnover < 3.0 and net_flow > 1000:
                action = "✅ 稳定封板"
                action_icon = "✅"
                analysis.append("涨停封死，主力续流入")
            elif opened_high and drawdown_from_high > 1.5:
                action = "⚠️ 涨停开板"
                action_icon = "⚠️"
                analysis.append(f"涨停打开！从{high:.2f}回落至{price:.2f}(-{drawdown_from_high}%)")
                if net_flow < 0:
                    analysis.append("主力同步流出，危险信号")
            elif turnover > 10:
                action = "⚠️ 巨量分歧"
                action_icon = "⚠️"
                analysis.append(f"换手{turnover}%巨量，多空分歧激烈")
            else:
                action = "🔴 涨停封板"
                action_icon = "🔴"
                analysis.append("涨停封死")
            
            # 今日不买（涨停无法介入）
            instruction = "⏳ 今日不买，等回踩"
            buy_zone = "待回踩确认"
            
        elif pct < -3 and net_flow > 500:
            action = "🎯 分歧低吸"
            action_icon = "🎯"
            instruction = f"买入 100股 ≈ {price*100:.0f}元"
            support = round(price * 0.97, 2)
            buy_zone = f"{support}-{price:.2f}"
            analysis.append(f"跌{pct}%但主力仍进{net_flow}万，分歧低吸机会")
            
        elif pct < -1 and net_flow > 0:
            action = "👀 微跌关注"
            action_icon = "👀"
            instruction = "观察等尾盘"
            buy_zone = f"{round(price*0.98,2)}-{price:.2f}"
            analysis.append(f"小跌{pct}%+主力微进{net_flow}万")
            
        elif pct > 3 and net_flow < -1000:
            action = "🚫 诱多出货"
            action_icon = "🚫"
            instruction = "放弃，不参与"
            buy_zone = "—"
            analysis.append(f"涨{pct}%但主力流出{net_flow}万，诱多嫌疑")
            
        elif net_flow > 5000 and pct > 5:
            action = "🔥 强势连板"
            action_icon = "🔥"
            instruction = "涨停不追，等分歧"
            buy_zone = "待回踩"
            analysis.append(f"主力{net_flow}万强势流入，连板中")
            
        else:
            action = "⏳ 中性观望"
            action_icon = "⏳"
            instruction = "信号不明，继续观察"
            buy_zone = "—"
            analysis.append(f"换手{turnover}% 量比{vol_ratio} 信号不明确")

        results.append({
            "code": code,
            "name": name,
            "price": price,
            "pct": pct,
            "turnover": turnover,
            "vol_ratio": vol_ratio,
            "main_inflow": net_flow,
            "inflow_delta": round(inflow_delta, 0),
            "high": high,
            "low": low,
            "open": open_p,
            "prev": prev,
            "amplitude": amplitude,
            "is_limit_up": is_limit_up,
            "limit_up_price": limit_up_price,
            "drawdown_from_high": drawdown_from_high,
            "action": action,
            "action_icon": action_icon,
            "instruction": instruction,
            "buy_zone": buy_zone,
            "analysis": analysis,
            "snap": snap,
        })

    return {
        "results": results,
        "limit_up_count": limit_up_count,
        "total": len(results),
        "buy_count": sum(1 for r in results if "买入" in r["instruction"]),
        "watch_count": sum(1 for r in results if "关注" in r["action"] or "观察" in r["action"]),
    }


def run(mode: str = MODE_EXECUTE) -> dict:
    """主入口。
    mode: 'snapshot' (09:35静默), 'execute' (10:15指令), 'close' (14:30尾盘)
    """
    print(f"[{now_str()}] 🐉 龙回头引擎启动 (mode={mode})...")

    # 读取追踪文件
    # 筛选活跃追踪（排除 ❌ 淘汰和 📦 归档的）
    pending = [s for s in load_tracker() 
               if "❌" not in s["status"] 
               and "淘汰" not in s["status"]
               and "归档" not in s["status"]
               and s["code"] not in ("—", "代码", "")]
    print(f"  📋 追踪池: {len(pending)} 只待观察")

    if not pending:
        # 诊断: 文件存在但没解析到?
        print(f"  🔍 tracker 存在: {os.path.exists(TRACKER_FILE)}")
        if os.path.exists(TRACKER_FILE):
            with open(TRACKER_FILE) as f:
                first_50 = f.read()[:500]
            print(f"  📄 文件前500字: {first_50}")
        print("  📭 无待观察标的")
        feishu_send(
            f"🐉 龙回头 | {now_str('%H:%M')}",
            f"📭 追踪池为空\n"
            f"tracker 存在: {os.path.exists(TRACKER_FILE)}\n"
            f"文件大小: {os.path.getsize(TRACKER_FILE) if os.path.exists(TRACKER_FILE) else 0} 字节"
        )
        return {"mode": mode, "pushed": True, "count": 0, "results": []}

    codes = [p["code"] for p in pending]
    prices = fetch_dragon_prices(codes)
    print(f"  💹 获取 {len(prices)} 只实时数据")

    if not prices:
        print("  ❌ 实时数据获取失败（行情API超时），仅输出 tracker 摘要")
        # 构造降级推送：精简每条 tracker 的关键信息
        lines = [f"🐉 龙回头执行指令 | {now_str('%m/%d %H:%M')}", ""]
        lines.append("⚠️ 实时行情 API 超时（已重试3次），以下为 tracker 关键摘要")
        lines.append("")
        # 限制总长度：每条最多输出 120 字，最多 5 只
        for p in pending[:5]:
            code = p["code"]
            raw = p.get("raw", [])
            name = raw[1] if len(raw) > 1 else "?"
            # 提取关键信息：前80字 + 价格/目标
            status = p["status"]
            # 只提取状态标记 + 最后一句（目标/止损）
            short_status = status.replace(" | ", "·")[:100].rstrip("，。.") 
            lines.append(f"• {code} {name}")
            lines.append(f"  {short_status}")
            lines.append("")
        if len(pending) > 5:
            lines.append(f"  ...等共 {len(pending)} 只")
        body = "\n".join(lines)
        # 飞书消息上限约 4000 字符，截断保护
        if len(body) > 3500:
            body = body[:3500] + "\n\n(消息过长已截断)"
        body += "\n" + "─" * 20 + "\n"
        body += f"⏰ {now_str('%H:%M')} | 追踪 {len(pending)}只 | 行情API超时(已重试3次)"
        feishu_send(f"🐉 龙回头 | {now_str('%m/%d %H:%M')}", body)
        return {"mode": mode, "pushed": True, "count": len(pending), "results": []}

    if mode == MODE_SNAPSHOT:
        # 静默快照，不推送
        snapshots = analyze_snapshot(prices)
        print(f"  📸 快照 {len(snapshots)} 只 → {SNAPSHOT_FILE}")
        return {"mode": mode, "pushed": False, "count": len(snapshots), "results": snapshots}

    elif mode == MODE_EXECUTE:
        analysis = analyze_execute(prices)
        results = analysis["results"]
        
        # ─── 构造丰富推送 ───
        lines = []
        
        # §1 标题
        lines.append(f"🐉 龙回头执行指令 | {now_str('%m/%d %H:%M')}")
        lines.append("")
        
        # §2 Day 2 回访 — 每只候选的实时数据+判定
        lines.append("━━━ 📊 Day 2 回访 ━━━")
        for r in results:
            pct_sign = "+" if r["pct"] >= 0 else ""
            flow_str = f"{r['main_inflow']/10000:.2f}亿" if abs(r['main_inflow']) >= 10000 else f"{r['main_inflow']:.0f}万"
            delta_str = ""
            if r["snap"]:
                d = r["inflow_delta"]
                delta_str = f" | 较09:35 {'+' if d>=0 else ''}{d:.0f}万"
            
            lines.append(f"• {r['action_icon']} **{r['name']}**({r['code']}) {r['price']:.2f} {pct_sign}{r['pct']:.2f}%")
            lines.append(f"  主力{flow_str} | 换手{r['turnover']:.1f}% | 量比{r['vol_ratio']:.2f}{delta_str}")
            if r["drawdown_from_high"] > 0.5:
                lines.append(f"  最高{r['high']:.2f}→现{r['price']:.2f}(上影{r['drawdown_from_high']:.1f}%)")
            for a in r["analysis"]:
                lines.append(f"  → {a}")
            lines.append(f"  判断: {r['action']} | {r['instruction']}")
            lines.append("")
        
        # §3 今日指令汇总
        lines.append("━━━ 🎯 今日指令 ━━━")
        buy_list = [r for r in results if "买入" in r["instruction"]]
        watch_list = [r for r in results if "关注" in r["action"] or "观察" in r["action"]]
        wait_list = [r for r in results if "不买" in r["instruction"] or "观望" in r["action"] or "不明" in r["instruction"]]
        
        if buy_list:
            for r in buy_list:
                lines.append(f"🎯 买入 **{r['name']}**({r['code']}) → {r['instruction']}")
                lines.append(f"   区间: {r['buy_zone']} | 现价: {r['price']:.2f}")
        else:
            lines.append("买入 0 只")
        
        if watch_list:
            for r in watch_list:
                lines.append(f"👀 关注 **{r['name']}**({r['code']}) → {r['instruction']}")
        if wait_list:
            names = "、".join([r['name'] for r in wait_list])
            lines.append(f"⏳ 等待: {names}（{'全涨停封死' if analysis['limit_up_count'] >= len(wait_list) else '信号不明'}，无分歧低吸窗口）")
        
        lines.append("")
        
        # §4 明日关注
        lines.append("━━━ 📅 明日关注 ━━━")
        tomorrow = (datetime.now(TZ) + timedelta(days=1))
        for r in results:
            if r["is_limit_up"]:
                target_high = round(r["price"] * 1.05, 2)
                dip_zone = f"{round(r['price'] * 0.92, 2)}-{round(r['price'] * 0.95, 2)}"
                lines.append(f"• {r['name']}({r['code']}) 今日涨停 {r['price']:.2f}")
                lines.append(f"  明日若回踩 {dip_zone} + 主力续流入 → 尾盘可考虑")
                lines.append(f"  目标: {target_high} | 100股 ≈ {r['price']*100:.0f}元")
            elif "买入" in r["instruction"]:
                lines.append(f"• {r['name']}({r['code']}) {r['instruction']}")
                lines.append(f"  区间: {r['buy_zone']}")
        
        lines.append("")
        body = "\n".join(lines)
        body += "─" * 20 + "\n"
        body += f"⏰ 龙回头执行引擎 {now_str('%H:%M')} | "
        body += f"追踪 {analysis['total']}只 | 涨停 {analysis['limit_up_count']}只 | "
        body += f"买入 {analysis['buy_count']} | 关注 {analysis['watch_count']}"

        title = f"🐉 龙回头执行指令 | {now_str('%m/%d %H:%M')}"
        result = feishu_send(title, body)
        pushed = result.get("success", False)

        # 有买入信号时额外推送
        buy_results = [r for r in results if "买入" in r["instruction"]]
        for r in buy_results:
            feishu_send(
                "🎯 龙回头买入信号",
                f"{r['name']}({r['code']})\n{r['analysis'][0]}\n区间: {r['buy_zone']} | 现价: {r['price']:.2f}"
            )

        print(f"  📤 推送 {'✅' if pushed else '❌'} | 买入 {analysis['buy_count']} | 关注 {analysis['watch_count']}")
        return {"mode": mode, "pushed": pushed, "count": analysis['total'],
                "buy": analysis['buy_count'], "watch": analysis['watch_count'],
                "limit_up": analysis['limit_up_count'], "results": results}

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