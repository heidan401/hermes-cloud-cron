#!/usr/bin/env python3
"""
交易引擎主线编排器 — 云端/本地通用入口
用法:
  python engine_runner.py 09:00    # 早报
  python engine_runner.py 09:30    # 风控
  python engine_runner.py 09:35    # 龙回头基准快照
  python engine_runner.py 10:00    # 通用反转
  python engine_runner.py 10:15    # 龙回头执行指令
  python engine_runner.py 14:30    # 龙回头尾盘
  python engine_runner.py 15:00    # 收盘简报
"""

import os
import sys
import json
import traceback
from datetime import datetime

from trading_engine.common import TZ, now_str, feishu_send

# 自动识别运行环境：GitHub Actions 设置 GITHUB_ACTIONS=true
IS_CLOUD = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

SLOTS = {
    "09:00": "morning_report",
    "09:30": "risk_control",
    "09:35": "dragon_snapshot",
    "10:00": "general_reversal",
    "10:15": "dragon_execute",
    "14:30": "dragon_close",
    "15:00": "close_report",
}


def run_morning_report() -> dict:
    from trading_engine.morning_report import run
    return run()


def run_risk_control() -> dict:
    from trading_engine.risk_control import run
    return run()


def run_dragon_snapshot() -> dict:
    from trading_engine.dragon_reversal import run
    return run(mode="snapshot")


def run_general_reversal() -> dict:
    from trading_engine.general_reversal import run
    return run()


def run_dragon_execute() -> dict:
    from trading_engine.dragon_reversal import run
    return run(mode="execute")


def run_dragon_close() -> dict:
    from trading_engine.dragon_reversal import run
    return run(mode="close")


def run_close_report() -> dict:
    """15:00 收盘简报 — 尾盘异动 + 明日关注"""
    try:
        import akshare as ak
        HAS_AK = True
    except ImportError:
        HAS_AK = False

    result = {"time": now_str(), "hot_sectors": [], "alerts": []}

    if not HAS_AK:
        return result

    try:
        # 涨幅前5板块
        df = ak.stock_board_concept_name_em()
        if df is not None and not df.empty:
            top5 = df.nlargest(5, "涨跌幅")
            for _, row in top5.iterrows():
                result["hot_sectors"].append({
                    "name": row["板块名称"],
                    "pct": float(row["涨跌幅"]),
                })
    except Exception:
        pass

    # 写 JSON 到 data/ 供云端第二天读取
    # 数据目录：engine_runner.py 的上两级 = github-actions-cron 仓库根
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(repo_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    json_path = os.path.join(data_dir, f"close_report_{datetime.now(TZ).strftime('%Y%m%d')}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  💾 收盘简报 → {json_path}")

    # 推送
    lines = []
    if result["hot_sectors"]:
        lines.append("📊 今日涨幅前十板块:")
        for s in result["hot_sectors"][:10]:
            lines.append(f"  • {s['name']}: {s['pct']:+.2f}%")

    body = "\n".join(lines) if lines else "📭 今日收盘数据获取不完整"
    body += "\n\n─" * 20 + "\n"
    body += f"⏰ 收盘简报 {now_str('%m/%d')} | 云端自动生成"

    feishu_send(f"🌙 收盘简报 | {now_str('%m/%d')}", body)

    return result


DISPATCH = {
    "morning_report": run_morning_report,
    "risk_control": run_risk_control,
    "dragon_snapshot": run_dragon_snapshot,
    "general_reversal": run_general_reversal,
    "dragon_execute": run_dragon_execute,
    "dragon_close": run_dragon_close,
    "close_report": run_close_report,
}


def main():
    if len(sys.argv) < 2:
        print("用法: python engine_runner.py <时点>")
        print("可选:", ", ".join(SLOTS.keys()))
        sys.exit(1)

    slot = sys.argv[1]
    task = SLOTS.get(slot)
    if not task:
        print(f"未知时点: {slot}")
        sys.exit(1)

    eta = datetime.now(TZ).replace(hour=int(slot[:2]), minute=int(slot[3:]), second=0)
    print(f"═══ {task} @ {slot} (预计 {eta.strftime('%H:%M')}) ═══")

    try:
        fn = DISPATCH.get(task)
        if not fn:
            print(f"  未实现: {task}")
            return

        result = fn()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"  ✅ {task} 完成")
    except Exception as e:
        print(f"  ❌ {task} 异常: {e}")
        traceback.print_exc()

        # 推送错误通知
        feishu_send(
            f"⚠️ 引擎异常 | {slot}",
            f"任务: {task}\n错误: {e}\n时间: {now_str()}"
        )


if __name__ == "__main__":
    main()