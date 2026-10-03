"""User command parsing (fast keyword path) and reply formatting."""

import re

PRIORITIES = ("P0", "P1", "P2", "P3")

QUERY_WORDS = ("查询", "待办", "列表", "list", "清单", "有什么")
HELP_WORDS = ("帮助", "help", "用法", "指令", "怎么用")
TODAY_WORDS = ("今日", "今天", "本日")
WEEK_WORDS = ("本周", "这周", "一周", "7天", "七天")
TRASH_WORDS = ("垃圾箱", "垃圾站", "回收站", "垃圾桶", "垃圾", "废纸篓")
RESTORE_WORDS = ("恢复", "还原", "找回")
EMPTY_WORDS = ("清空", "清光", "全部清", "都清", "全清")
DELETE_WORDS = ("清除", "删除", "清掉", "丢弃", "丢掉", "移除")

# Fast commands are short by nature. Anything longer is treated as content
# for the LLM, so forwarded homework/notices are never misread as a command.
# Note: completion intent is intentionally NOT handled here — it goes through
# the LLM so it can judge whether the user really means "this is done".
MAX_COMMAND_LEN = 60


def parse(text):
    t = text.strip()
    low = t.lower()

    if len(t) > MAX_COMMAND_LEN:
        return None

    if any(w in low for w in HELP_WORDS) and len(t) <= 6:
        return {"cmd": "help"}

    # empty the whole trash: must be checked before plain "trash"
    if any(w in low for w in TRASH_WORDS) and any(w in low for w in EMPTY_WORDS):
        return {"cmd": "empty_trash"}

    # delete specific items (numbers) from trash
    if any(w in low for w in DELETE_WORDS):
        ids = [int(x) for x in re.findall(r"\d+", t)]
        return {"cmd": "delete", "ids": ids}

    if any(w in low for w in TRASH_WORDS):
        return {"cmd": "trash"}

    if any(w in low for w in RESTORE_WORDS):
        m = re.search(r"(\d+)", t)
        if m:
            return {"cmd": "restore", "id": int(m.group(1))}
        keyword = t
        for w in RESTORE_WORDS:
            keyword = keyword.replace(w, " ")
        keyword = keyword.strip(" ：:，,。.!！的把")
        return {"cmd": "restore", "keyword": keyword or None}

    # query
    is_query = any(w in low for w in QUERY_WORDS)
    priority = next((p for p in PRIORITIES if p in t.upper()), None)
    rng = None
    if any(w in low for w in TODAY_WORDS):
        rng = "today"
        is_query = True
    elif any(w in low for w in WEEK_WORDS):
        rng = "week"
        is_query = True
    if priority:
        is_query = True
    if is_query:
        return {"cmd": "query", "priority": priority, "range": rng}
    return None


def fmt_due(due):
    if not due:
        return ""
    s = due.replace("T", " ")
    try:
        s = s[:16]
    except Exception:  # noqa: BLE001
        pass
    return s


def format_task(task, with_id=True):
    due = fmt_due(task.get("due_at"))
    parts = [f"[{task['priority']}]", task["title"]]
    if task.get("recur"):
        parts.append("↻")
    if due:
        parts.append(due)
    if with_id:
        parts.append(f"(#{task['id']})")
    return " ".join(parts)


REPEAT_LABEL = {"daily": "每天", "weekly": "每周", "monthly": "每月"}


def format_ack(priority, title, due, repeat=None):
    """Format the mandatory acknowledgement: [P几],事件名,时间,已被记录"""
    bits = [f"[{priority}]", title]
    if due:
        bits.append(fmt_due(due))
    if repeat:
        label = REPEAT_LABEL.get(repeat.get("freq"), "重复")
        interval = repeat.get("interval", 1)
        bits.append(f"{label}重复{'(每'+str(interval)+'期)' if interval > 1 else ''}")
    return ", ".join(bits) + ", 已被记录"


def format_query(tasks, title="当前待办"):
    if not tasks:
        return "没有待办事项。"
    lines = [f"📋 {title}（{len(tasks)}）"]
    for t in tasks:
        lines.append("· " + format_task(t))
    return "\n".join(lines)


HELP_TEXT = (
    "用法：\n"
    "· 直接发消息记录事项，例：明天下午3点交高数作业\n"
    "· 重复：往后30天每天晚上十一点提醒我喷药\n"
    "· 查询：查询 / 今日 / 本周 / P0 / 待办\n"
    "· 完成：完成 3（编号）或 完成了高数作业\n"
    "· 修改：把3改到10号23:59\n"
    "· 垃圾箱：垃圾箱 / 恢复 3 / 清除 3 / 清空垃圾箱\n"
    "· 播报时间：查看或「播报时间改成 08:30 和 23:30」\n"
    "· 帮助：帮助\n\n"
    "优先级：P0=48小时内紧急  P1=作业/有截止  P2=重要不紧急  P3=备忘"
)