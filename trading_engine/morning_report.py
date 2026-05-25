"""
09:00 ☁️ 炒股早报 — 隔夜资讯聚合 + LLM 板块异动预判
云端独立运行，不依赖本地 Hermes。
只跑一次，数据可被后续管线复用。
"""

import json
import os
import sys
from datetime import datetime, timedelta

from trading_engine.common import TZ, now_str, today_str, dashscope_chat, feishu_send

# akshare 在 GitHub Actions 环境安装后可用
try:
    import akshare as ak
    HAS_AK = True
except ImportError:
    HAS_AK = False


def fetch_market_overview() -> dict:
    """获取全市场概况数据 + 前日收盘简报"""
    result = {
        "time": now_str(),
        "overnight_us": None,
        "overnight_a50": None,
        "sectors_top5": [],
        "hot_concepts": [],
        "yesterday_close": None,  # 前日收盘简报
    }

    if not HAS_AK:
        return result

    # ─── 读取前日收盘简报 ───
    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y%m%d")
    close_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", f"close_report_{yesterday}.json")
    if os.path.exists(close_file):
        try:
            with open(close_file) as f:
                result["yesterday_close"] = json.load(f)
        except Exception:
            pass

    try:
        # 美股隔夜（前一日收盘）
        df_us = ak.index_us_stock_sina(symbol=".DJI")
        if df_us is not None and not df_us.empty:
            latest = df_us.iloc[-1]
            result["overnight_us"] = {
                "index": "道琼斯",
                "close": float(latest.get("close", 0)),
                "pct_change": float(latest.get("pct_chg", 0))
            }
    except Exception:
        pass

    try:
        # A50 期货
        df_a50 = ak.futures_zh_minute_sina(symbol="CN")
        if df_a50 is not None and not df_a50.empty:
            latest = df_a50.iloc[-1]
            result["overnight_a50"] = {
                "index": "A50期货",
                "price": float(latest.get("price", 0)),
                "pct_change": float(latest.get("pct_chg", 0))
            }
    except Exception:
        pass

    try:
        # A股板块涨幅前5
        df_sector = ak.stock_board_concept_name_em()
        if df_sector is not None and not df_sector.empty:
            top5 = df_sector.sort_values("涨跌幅", ascending=False).head(5)
            for _, row in top5.iterrows():
                result["sectors_top5"].append({
                    "name": row["板块名称"],
                    "pct": float(row["涨跌幅"]),
                    "lead_stock": row.get("领涨股票", "")
                })
    except Exception:
        pass

    return result


def fetch_overnight_news() -> list:
    """获取隔夜重要新闻 """
    news = []
    try:
        if HAS_AK:
            df_news = ak.stock_info_global_em()
            if df_news is not None and not df_news.empty:
                for _, row in df_news.head(10).iterrows():
                    news.append(row.get("title", "") or row.get("content", ""))
    except Exception:
        pass
    return news[:8]


def build_prompt(data: dict, news: list) -> str:
    """构造 LLM prompt"""
    parts = ["以下为今日 A股盘前数据，请用 200 字内总结核心关注点和板块风险："]

    if data.get("overnight_us"):
        us = data["overnight_us"]
        parts.append(f"隔夜美股道琼斯: {us['pct_change']:+.2f}%")

    if data.get("overnight_a50"):
        a50 = data["overnight_a50"]
        parts.append(f"A50期货: {a50['pct_change']:+.2f}%")

    if data["sectors_top5"]:
        parts.append("\n板块涨幅前5:")
        for s in data["sectors_top5"]:
            parts.append(f"  {s['name']}: {s['pct']:+.2f}%")

    # 前日收盘简报
    if data.get("yesterday_close"):
        yc = data["yesterday_close"]
        if yc.get("hot_sectors"):
            parts.append("\n📊 昨日收盘板块回顾:")
            for s in yc["hot_sectors"][:5]:
                parts.append(f"  {s['name']}: {s['pct']:+.2f}%")
        if yc.get("alerts"):
            parts.append("昨日异动:")
            for a in yc["alerts"][:3]:
                parts.append(f"  • {a}")

    if news:
        parts.append("\n隔夜要闻:")
        for i, n in enumerate(news[:5], 1):
            parts.append(f"  {i}. {n}")

    return "\n".join(parts)


def run() -> dict:
    """主入口 — 返回 {"pushed": bool, "report": str}"""
    print(f"[{now_str()}] 🚀 早报引擎启动...")

    data = fetch_market_overview()
    news = fetch_overnight_news()

    if not data["overnight_us"] and not news:
        print("  ⚠️ 无隔夜数据，跳过推送")
        return {"pushed": False, "report": ""}

    prompt = build_prompt(data, news)
    print(f"  📡 调用 LLM 生成早报...")

    report = dashscope_chat(prompt)

    # 推送到飞书
    title = f"📰 炒股早报 | {now_str('%m/%d %H:%M')}"
    body = report + "\n\n"
    body += "─" * 20 + "\n"
    body += "📍 数据源：akshare | ⏰ 09:00 自动推送"

    result = feishu_send(title, body)
    print(f"  📤 飞书推送: {'✅' if result.get('success') else '❌ ' + str(result.get('error', ''))}")

    return {"pushed": result.get("success", False), "report": report}


# ─── CLI 入口 ──────────────────────────────────────
if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))