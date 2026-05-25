#!/usr/bin/env python3
"""
GitHub Actions 早盘扫描 — 云端兜底版
======================================
不依赖 Mac，在 GitHub Actions 上定时运行。
获取 A 股数据 → LLM 分析 → 推送飞书。

用法: python morning_scan.py [--time 09:35|10:00|10:15|14:30]
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta
from typing import Optional

import requests
import akshare as ak

# ============================================================
# 配置 — 全部从环境变量读取（GitHub Secrets）
# ============================================================
FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
DASHSCOPE_API_KEY = os.environ["DASHSCOPE_API_KEY"]
FEISHU_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_96c6da321fbda97d7687b2afadeff808")

# 选股约束
MAX_PRICE = float(os.environ.get("MAX_PRICE", "25"))
MIN_MAIN_INFLOW = float(os.environ.get("MIN_MAIN_INFLOW", "2000"))  # 万元
EXCLUDE_PREFIXES = ("300", "301", "688", "8", "N", "C")  # 创业板/科创板/北交所/新股


# ============================================================
# 飞书 API
# ============================================================
def get_feishu_token() -> str:
    """获取 tenant_access_token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET
    }, timeout=15)
    return resp.json()["tenant_access_token"]


def send_feishu(text: str) -> bool:
    """发送消息到飞书"""
    token = get_feishu_token()
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    payload = {
        "receive_id": FEISHU_CHAT_ID,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False)
    }
    resp = requests.post(url, json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }, timeout=15)
    data = resp.json()
    if data.get("code") != 0:
        print(f"飞书发送失败: {data}")
        return False
    return True


# ============================================================
# LLM 分析
# ============================================================
def call_llm(prompt: str) -> str:
    """调用 DashScope (阿里百炼) LLM"""
    import dashscope
    dashscope.api_key = DASHSCOPE_API_KEY
    from dashscope import Generation
    
    resp = Generation.call(
        model="qwen-plus",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        temperature=0.1
    )
    if resp.status_code == 200:
        return resp.output.choices[0].message.content
    else:
        return f"LLM 调用失败: {resp.message}"


# ============================================================
# A 股数据
# ============================================================
def get_stock_rank(top_n: int = 100) -> list:
    """
    获取主力资金净流入排名 top N。
    返回: [{code, name, price, main_inflow, change_pct, turnover, volume_ratio, pe, market_cap}, ...]
    """
    try:
        # 实时资金流向排名
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
        if df is None or df.empty:
            print("资金流向数据为空")
            return []
        
        # 取主力净流入 top N
        df = df.sort_values("主力净流入-净额", ascending=False).head(top_n)
        
        results = []
        for _, row in df.iterrows():
            code = str(row.get("代码", ""))
            # 过滤
            if any(code.startswith(p) for p in EXCLUDE_PREFIXES):
                continue
            price = float(row.get("最新价", 0))
            if price > MAX_PRICE or price <= 0:
                continue
            
            results.append({
                "code": code,
                "name": row.get("名称", ""),
                "price": price,
                "main_inflow": float(row.get("主力净流入-净额", 0)) / 10000,  # 转万元
                "change_pct": float(row.get("涨跌幅", 0)),
                "turnover": float(row.get("换手率", 0)),
                "volume_ratio": float(row.get("量比", 0)),
                "pe": float(row.get("市盈率-动态", 0) or 0),
                "market_cap": float(row.get("流通市值", 0) or 0) / 1e8,  # 转亿
            })
        return results
    except Exception as e:
        print(f"获取排名数据失败: {e}")
        return []


def get_stock_history(code: str, days: int = 5) -> list:
    """获取个股近 N 日主力资金流向"""
    try:
        df = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz")
        if df is None or df.empty:
            return []
        recent = df.tail(days)
        return [float(x) / 10000 for x in recent["主力净流入-净额"].tolist()]
    except Exception as e:
        print(f"获取 {code} 历史资金失败: {e}")
        return []


def get_stock_kdj(code: str) -> dict:
    """获取 KDJ 指标"""
    try:
        market = "sh" if code.startswith("6") else "sz"
        symbol = f"{market}{code}"
        # 用日K计算 KDJ
        df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
        if df is None or df.empty:
            return {}
        
        # 简单 KDJ 计算
        recent = df.tail(9)
        low = recent["最低"].min()
        high = recent["最高"].max()
        close = recent["收盘"].iloc[-1]
        
        if high == low:
            rsv = 50
        else:
            rsv = (close - low) / (high - low) * 100
        
        # 简化 K=RSV, D=RSV, J=RSV
        return {"k": round(rsv, 1), "d": round(rsv, 1), "j": round(rsv, 1)}
    except Exception as e:
        print(f"获取 {code} KDJ 失败: {e}")
        return {}


# ============================================================
# 龙回头扫描
# ============================================================
def scan_dragon_reversal() -> str:
    """
    扫描龙回头 Day 1 候选：
    1. 获取主力流入 top 100
    2. 过滤创业板/科创板/ST/高价/大市值
    3. 对每只查近5日资金 → 找连续流出后今日反转的
    """
    print("📊 获取主力资金排名...")
    stocks = get_stock_rank(100)
    print(f"  初筛 {len(stocks)} 只（已过滤创业板/科创板/高价）")
    
    candidates = []
    for s in stocks:
        history = get_stock_history(s["code"], 5)
        if not history or len(history) < 4:
            continue
        
        # 前 4 日必须全部 < 0（流出）或至少 3/4 流出
        prev_flows = history[:-1]
        today_flow = history[-1]
        
        out_days = sum(1 for f in prev_flows if f < 0)
        if out_days >= 3 and today_flow > MIN_MAIN_INFLOW:
            s["prev_flows"] = prev_flows
            s["today_flow"] = today_flow
            s["out_days"] = out_days
            candidates.append(s)
    
    print(f"  龙回头候选: {len(candidates)} 只")
    
    # 取 top 5
    candidates = sorted(candidates, key=lambda x: x["today_flow"], reverse=True)[:5]
    
    # 组装报告
    if not candidates:
        return "今日无龙回头 Day 1 候选。"
    
    lines = ["🐉 龙回头 Day 1 候选扫描\n"]
    for i, c in enumerate(candidates, 1):
        prev_str = " → ".join([f"{f:+.0f}万" for f in c["prev_flows"]])
        lines.append(
            f"{'🥇🥈🥉'[i-1] if i <= 3 else str(i)} {c['name']}({c['code']})\n"
            f"  现价 {c['price']} | 今日主力 {c['today_flow']:+.0f}万 | 涨幅 {c['change_pct']:+.1f}%\n"
            f"  前4日: {prev_str} ({c['out_days']}/4 流出)\n"
            f"  换手 {c['turnover']:.1f}% | 量比 {c['volume_ratio']:.1f} | PE {c['pe']:.0f}"
        )
    
    # LLM 点评
    prompt = f"""你是A股短线交易分析助手。以下是今日龙回头(反转策略)候选股：

{chr(10).join(lines)}

请用简洁中文分析（200字内）：
1. 哪只反转逻辑最强？
2. 有哪些风险点需要注意？
3. 今日是否适合建仓？
"""
    analysis = call_llm(prompt)
    lines.append(f"\n\n🤖 AI 点评:\n{analysis}")
    
    return "\n".join(lines)


# ============================================================
# 主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--time", default="09:35", help="任务时间点")
    args = parser.parse_args()
    
    now = datetime.now()
    print(f"🚀 早盘扫描启动 | {now.strftime('%Y-%m-%d %H:%M')} | 任务: {args.time}")
    
    # 检查是否是交易日（周一到周五）
    if now.weekday() >= 5:
        print("⏭️ 非交易日，跳过")
        return
    
    # 执行扫描
    report = scan_dragon_reversal()
    
    # 组装推送内容
    header = f"☁️ [云端兜底] 龙回头扫描 | {now.strftime('%m/%d %H:%M')}"
    full_msg = f"{header}\n\n{report}"
    
    # 推送飞书
    print("📤 推送飞书...")
    if send_feishu(full_msg):
        print("✅ 推送成功")
    else:
        print("❌ 推送失败")
    
    # 同时打印到 stdout（GitHub Actions 日志）
    print(f"\n{'='*50}")
    print(full_msg)


if __name__ == "__main__":
    main()