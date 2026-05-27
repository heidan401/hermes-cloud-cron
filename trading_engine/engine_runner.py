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
    "11:00": "general_reversal",
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
    """15:00 收盘简报 — 大盘+板块+龙回头+持仓 全量汇总"""
    import time as _time
    import subprocess as _sp
    import glob as _glob
    
    # 路径解析（一次定义，全函数复用）
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _talent_dir = os.path.dirname(_repo_root)  # ~/天才交易员
    
    try:
        import akshare as ak
        HAS_AK = True
    except ImportError:
        HAS_AK = False

    result = {"time": now_str(), "indices": [], "hot_sectors": [], 
              "dragon_summary": "", "holdings_summary": ""}

    # ─── 1. 大盘指数 ───
    try:
        idx_out = _sp.run(["stock", "index"], capture_output=True, text=True, timeout=15)
        if idx_out.returncode == 0:
            result["indices"] = idx_out.stdout.strip()
    except Exception:
        pass

    # ─── 2. 板块数据：akshare 三重试 + stock CLI 兜底 ───
    sectors_fetched = False
    if HAS_AK:
        for attempt in range(3):
            try:
                df = ak.stock_board_concept_name_em()
                if df is not None and not df.empty and len(df) > 10:
                    top5 = df.nlargest(5, "涨跌幅")
                    bottom5 = df.nsmallest(5, "涨跌幅")
                    for _, row in top5.iterrows():
                        result["hot_sectors"].append({
                            "name": row["板块名称"],
                            "pct": float(row["涨跌幅"]),
                        })
                    result["cold_sectors"] = []
                    for _, row in bottom5.iterrows():
                        result["cold_sectors"].append({
                            "name": row["板块名称"], 
                            "pct": float(row["涨跌幅"]),
                        })
                    sectors_fetched = True
                    print(f"  ✅ 板块数据: akshare 第{attempt+1}次成功")
                    break
                else:
                    print(f"  ⚠️ akshare 第{attempt+1}次返回空/不足，{'重试...' if attempt < 2 else '放弃'}")
                    if attempt < 2:
                        _time.sleep(5)
            except Exception as e:
                print(f"  ⚠️ akshare 第{attempt+1}次异常: {e}")
                if attempt < 2:
                    _time.sleep(5)

    # ─── 3. 龙回头 14:30 扫描结果读取 ───
    try:
        longhu_dir = os.path.join(_talent_dir, "龙回头")
        patterns = [os.path.join(longhu_dir, f"{datetime.now(TZ).strftime('%Y%m%d')}*尾盘*.md"),
                     os.path.join(longhu_dir, f"{datetime.now(TZ).strftime('%Y%m%d')}*扫描*.md")]
        for pat in patterns:
            files = sorted(_glob.glob(pat))
            if files:
                with open(files[-1]) as f:
                    content = f.read()
                    result["dragon_summary"] = content[:800] if len(content) > 800 else content
                break
    except Exception:
        pass

    # ─── 4. 持仓汇总 ───
    try:
        holdings_path = os.path.join(_talent_dir, "holdings.md")
        if os.path.exists(holdings_path):
            with open(holdings_path) as f:
                result["holdings_summary"] = f.read()[:500]
    except Exception:
        pass

    # ─── 5. 落盘 Markdown 报告 ───
    today = datetime.now(TZ).strftime("%Y%m%d")
    report_dir = os.path.join(_talent_dir, "收盘")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"{today}_1500_收盘简报.md")
    
    md = f"# 🌙 收盘简报 | {today}\n\n"
    md += f"## 大盘指数\n```\n{result['indices']}\n```\n\n" if result['indices'] else "## 大盘指数\n⚠️ 获取失败\n\n"
    
    if result["hot_sectors"]:
        md += "## 🔥 涨幅前5板块\n"
        for s in result["hot_sectors"]:
            md += f"• {s['name']}: **{s['pct']:+.2f}%**\n"
    else:
        md += "## 🔥 板块数据\n⚠️ akshare 收盘后限流，板块数据获取失败\n"
    
    if result.get("cold_sectors"):
        md += "\n## ❄️ 跌幅前5板块\n"
        for s in result["cold_sectors"]:
            md += f"• {s['name']}: {s['pct']:+.2f}%\n"
    
    if result["dragon_summary"]:
        md += f"\n## 🐉 龙回头尾盘\n{result['dragon_summary']}\n"
    else:
        md += "\n## 🐉 龙回头尾盘\n⚠️ 未找到今日尾盘扫描文件（c123a5614791 可能未执行）\n"
    
    md += f"\n---\n⏰ 自动生成 {now_str()}\n"
    
    with open(report_path, "w") as f:
        f.write(md)
    print(f"  💾 收盘简报 → {report_path}")

    # ─── 6. JSON 数据（兼容旧格式，供云端早报消费） ───
    data_dir = os.path.join(_repo_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    json_path = os.path.join(data_dir, f"close_report_{today}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # ─── 7. 飞书推送 ───
    lines = []
    if result["indices"]:
        lines.append("📊 大盘指数:")
        for line in result["indices"].split("\n")[:4]:
            if "上证" in line or "深证" in line or "创业" in line or "科创" in line:
                lines.append(f"  {line.strip()}")
    if result["hot_sectors"]:
        lines.append("\n🔥 涨幅前5:")
        for s in result["hot_sectors"][:5]:
            lines.append(f"  • {s['name']}: {s['pct']:+.2f}%")
    else:
        lines.append("\n⚠️ 板块数据获取失败（收盘后限流）")
    lines.append(f"\n📄 完整报告 → 收盘/{today}_1500_收盘简报.md")
    
    body = "\n".join(lines)
    feishu_send(f"🌙 收盘简报 | {today}", body)
    
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