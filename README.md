# QQ 待办助理

把你的 QQ 单聊机器人接上大模型，自动把消息分成 P0–P3，存进 `memo.db`，通过
ntfy 推送到手机；支持在 QQ 里主动查询和标记完成。

## 功能

- 官方 QQ 机器人（群/单聊开放平台，`AppID` + `AppSecret`），WebSocket 长连接，无需公网 IP
- 大模型（阿里云百炼 OpenAI 兼容接口）做意图识别与优先级分类
  - **P0**：48 小时内必须完成的紧急任务
  - **P1**：课程作业、有明确截止的学业/工作任务
  - **P2**：重要但不紧急（长期项目、考试/面试、健康、财务等）
  - **P3**：信息记录、低优先级、可延后
- 每条消息回复：`[P几], 事件名, 时间, 已被记录`
- ntfy 每日汇总 + 事件前提醒（P0 提前 1 天 + 2 小时，P1 提前 1 天）
- 手机端主动查询：在 QQ 里发指令
- 标记完成：`完成 3` / `完成了高数作业`
- 存储：标准库 sqlite3，单文件 `memo.db`

## 前置准备

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人，拿到 `AppID` / `AppSecret`
2. 在管理端申请 **单聊** 场景与 `GROUP_AND_C2C_EVENT` (`1<<25`) 事件权限
3. 手机安装 [ntfy](https://ntfy.sh/) App，订阅一个自定义 topic（如 `qq-todo-xxxx`）

## 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入 app_id / app_secret / llm.api_key / ntfy.topic
```

## 运行

```bash
./run.sh
```

或使用 systemd：

```bash
sudo cp qq-agent.service /etc/systemd/system/qq-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now qq-agent
sudo systemctl status qq-agent
```

## QQ 指令

| 指令 | 说明 |
| --- | --- |
| 直接发消息 | 记录事项，例：`明天下午3点交高数作业` |
| `查询` / `待办` | 列出所有待办 |
| `今日` / `本周` | 按截止时间筛选 |
| `P0` / `P1` … | 按优先级筛选 |
| `完成 3` | 按编号完成 |
| `完成了高数作业` | 按关键词完成 |
| `帮助` | 查看用法 |

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `main.py` | 入口，串联 QQ / LLM / 存储 / 调度 |
| `qqbot.py` | 令牌刷新、WebSocket 网关、C2C 收发 |
| `llm.py` | 分类、意图识别、每日汇总 |
| `db.py` | `memo.db` 建表与读写 |
| `notify.py` | ntfy 推送 |
| `scheduler.py` | 每日汇总 + 事件前提醒 |
| `commands.py` | 关键词指令解析与回复格式化 |

## 说明

- QQ 主动消息有频控且用户可关闭，因此推送统一走 ntfy；QQ 只做被动回复。
- bot 的 WebSocket 必须在线；离线期间的消息会在重连后补发（单聊 60 分钟内）。