# ☁️ Hermes 云端兜底

GitHub Actions 定时运行 A 股早盘扫描，**不依赖 Mac**。

当你的 Mac 关机/休眠/没网时，云端照样跑，结果推飞书。

## 工作原理

```
GitHub Actions (北京时间 09:35/10:00/10:15/14:30)
  → akshare 拉取 A 股主力资金排名
  → 龙回头反转逻辑筛选
  → DashScope (阿里百炼) LLM 点评
  → 飞书消息推送
```

## 第一次配置

### 1. 创建 GitHub 仓库

```bash
# 在本目录执行
git init
git add .
git commit -m "init: 云端兜底早盘扫描"
gh repo create hermes-cloud-cron --public --push
```

### 2. 设置 Secrets

在 GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Secret | 值 | 说明 |
|--------|-----|------|
| `FEISHU_APP_ID` | `cli_a96b6181...` | 飞书应用 ID |
| `FEISHU_APP_SECRET` | `你的飞书密钥` | 飞书应用密钥 |
| `DASHSCOPE_API_KEY` | `你的百炼 Key` | 阿里百炼 API Key |
| `FEISHU_CHAT_ID` | `oc_96c6da...` | 推送目标群 ID |

### 3. 验证

进入 GitHub Actions 页面，手动触发一次 `workflow_dispatch`，确认飞书收到消息。

## 时间线

| 任务 | 北京时间 | UTC |
|------|----------|-----|
| 🐉 龙回头观察池 | 09:35 | 01:35 |
| 📊 Alpha Lab 整点 | 10:00 | 02:00 |
| ⚡ 执行指令 | 10:15 | 02:15 |
| 🧹 尾盘扫描 | 14:30 | 06:30 |

## 与 Mac 端 Hermes 的关系

- **互补，不冲突**。两边都会跑，飞书可能收到两条消息（云端 + Mac 端）
- Mac 端做完整深度分析，云端做轻量兜底扫描
- 你出门在外、Mac 没开 → 云端保证你不错过早盘