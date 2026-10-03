<div align="center">

# 📌 QQ 待办助理

**把 QQ 聊天变成你的智能待办：自动分优先级、存本地、推到手机。**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![QQ Bot](https://img.shields.io/badge/QQ-%E6%9C%BA%E5%99%A8%E4%BA%BA-12B7F5.svg)](https://q.qq.com/)
[![ntfy](https://img.shields.io/badge/Push-ntfy-57AEE0.svg)](https://ntfy.sh/)

用 [腾讯 QQ 开放平台](https://q.qq.com/) 的官方机器人接收消息，交给大模型拆解成
**P0–P3** 待办，存进单文件 `memo.db`，并通过 [ntfy](https://ntfy.sh/) 把每日汇总和
到点提醒推到你手机。整个过程只需一个 QQ 单聊窗口。

</div>

---

## ✨ 特性

| | |
| --- | --- |
| 🤖 **QQ 原生** | 官方单聊机器人，WebSocket 长连接，**无需公网 IP / 域名** |
| 🧠 **大模型分诊** | 阿里云百炼（OpenAI 兼容）自动识别意图与优先级，长转发文也不会被误判 |
| 📋 **智能拆分** | 一条转发的作业清单自动拆成多条独立待办 |
| ⏰ **周期任务** | 支持每天 / 每周 / 每月重复，到点提醒，可选重复截止日 |
| 📲 **ntfy 推送** | 每日汇总 + 事件前提醒，手机上即时接收 |
| 🗑️ **垃圾箱** | 过期 3 小时自动入垃圾箱，不再打扰；可查询 / 恢复 / 清除 |
| ✏️ **可修改** | 直接说「整错了，是10号23:59前」即可改时间 |
| 🪶 **轻量存储** | 标准库 `sqlite3` 单文件，无重型数据库 |
| 🐧 **零依赖部署** | 纯 Python + systemd，开箱即用 |

### 优先级规则

| 级别 | 含义 | 提醒策略 |
| :---: | --- | --- |
| **P0** | 48 小时内必须完成的紧急任务 | 提前 1 天 + 提前 2 小时 |
| **P1** | 课程作业、有明确截止的学业/工作 | 提前 1 天；**进入 48 小时窗口自动升为 P0** |
| **P2** | 重要不紧急（长期项目、考试、健康…） | 仅每日汇总 |
| **P3** | 备忘、低优先级 | 仅每日汇总 |

---

## 🚀 快速开始

### 1. 前置准备

- 在 [QQ 开放平台](https://q.qq.com/) 创建机器人，获取 `AppID` / `AppSecret`
- 在管理端申请 **单聊** 场景与 `GROUP_AND_C2C_EVENT` (`1<<25`) 事件权限
- 手机安装 [ntfy](https://ntfy.sh/) App，订阅一个自定义 topic（如 `qq-todo-xxxxxxxx`）

> Android 用户建议使用 **F-Droid 版** ntfy：Google Play 版对 `ntfy.sh` 依赖 FCM，国内后台推送不可用。

### 2. 安装与配置

```bash
git clone https://github.com/tony20071026/qq-todo-agent.git
cd qq-todo-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml
# 编辑 config.yaml 填入 app_id / app_secret / llm.api_key / ntfy.topic
```

### 3. 启动

```bash
./run.sh                     # 前台运行
```

或使用 systemd 常驻（开机自启）：

```bash
sudo cp qq-agent.service /etc/systemd/system/qq-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now qq-agent
systemctl status qq-agent
```

常用运维：

```bash
systemctl restart qq-agent          # 改完配置后重启
journalctl -u qq-agent -f           # 实时日志
tail -f /root/test_for_qq_agent/agent.log
```

---

## 💬 QQ 指令

直接发消息即可记录，机器人会回复 `[P几], 事件名, 时间, 已被记录`。

| 场景 | 指令示例 |
| --- | --- |
| 记录事项 | `明天下午3点交高数作业` |
| 转发清单 | 直接转发老师/群里的作业通知（自动拆条） |
| 周期任务 | `往后30天每天晚上十一点提醒我喷药` |
| 查询全部 | `查询` / `待办` |
| 按时间筛选 | `今日` / `本周` |
| 按优先级筛选 | `P0` / `P1` / `P2` / `P3` |
| 标记完成 | `完成 3` 或 `完成了高数作业` |
| 修改事项 | `把3改到10号23:59` / `整错了 是10号23:59前` |
| 查看垃圾箱 | `垃圾箱` |
| 恢复事项 | `恢复 3` |
| 彻底删除 | `清除 3` / `清除 3 4` |
| 清空垃圾箱 | `清空垃圾箱` |
| 查看用法 | `帮助` |

---

## 🏗️ 工作原理

```
        发消息 / 转发清单
              │
              ▼
   ┌────────────────────┐   WebSocket 长连接
   │  QQ 开放平台机器人  │◀──────────────────┐
   └─────────┬──────────┘                   │
             │ C2C_MESSAGE_CREATE           │ 被动回复
             ▼                              │
   ┌────────────────────┐   intent / 拆分   │
   │   llm.py  大模型    │──────┐           │
   └────────────────────┘      ▼           │
                        ┌───────────────┐   │
                        │    main.py    │───┘
                        │  意图路由/回复 │
                        └──────┬────────┘
                               ▼
                     ┌──────────────────┐
                     │ memo.db (sqlite) │
                     └────────┬─────────┘
                              ▼
                 ┌────────────────────────┐
                 │ scheduler.py 每日/到点  │──▶ ntfy ──▶ 📱 手机
                 └────────────────────────┘
```

| 文件 | 职责 |
| --- | --- |
| `main.py` | 入口，串联各模块与消息路由 |
| `qqbot.py` | 令牌刷新、WebSocket 网关、C2C 收发 |
| `llm.py` | 意图识别、优先级分类、每日汇总 |
| `db.py` | `memo.db` 建表、迁移与读写 |
| `notify.py` | ntfy 推送封装 |
| `scheduler.py` | 每日汇总、事件提醒、周期推进、垃圾箱 |
| `commands.py` | 快捷指令解析与回复格式化 |

---

## ⚙️ 配置项

```yaml
qq:
  app_id: "YOUR_APP_ID"
  app_secret: "YOUR_APP_SECRET"
  intents: 33554432            # 1<<25 = GROUP_AND_C2C_EVENT

llm:
  base_url: "https://.../compatible-mode/v1"
  api_key: "YOUR_DASHSCOPE_API_KEY"
  model: "deepseek-v4.1-flash" # 任意百炼 OpenAI 兼容模型

ntfy:
  server: "https://ntfy.sh"
  topic: "YOUR_RANDOM_TOPIC"
  token: ""                    # 受保护 topic 时填写

app:
  timezone: "Asia/Shanghai"
  db_path: "memo.db"
  digest_time: "08:00"         # 每日汇总时间
  trash_after_hours: 3         # 过期多久入垃圾箱
  reminders:                   # 各优先级提前提醒（分钟）
    P0: [1440, 120]
    P1: [1440]
    P2: []
    P3: []
  tick: 30                     # 调度轮询间隔（秒）
```

---

## 🔐 安全与隐私

- `config.yaml`（含密钥）与 `memo.db`（含数据）均已加入 `.gitignore`，**不会进入仓库**
- 迁移历史数据时请手动拷贝 `memo.db`
- ntfy 公共 topic 只要知道名字即可读取，请勿外传；也可自建 ntfy 并启用 `token`

## 🤝 说明

- QQ 主动消息有频控且用户可关闭，因此推送统一走 ntfy，QQ 仅做被动回复
- Bot 的 WebSocket 需保持在线；离线期间消息会在重连后补发（单聊 60 分钟内）

## 📄 License

本项目遵循 [GNU General Public License v3.0](LICENSE)（GPL-3.0）。

<div align="center"><sub>Made with ❤️ for a tidier day.</sub></div>