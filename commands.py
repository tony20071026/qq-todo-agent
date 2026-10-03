"""User command parsing (fast keyword path) and reply formatting."""

import re

PRIORITIES = ("P0", "P1", "P2", "P3")

QUERY_WORDS = ("查询", "待办", "列表", "list", "清单", "有什么")
DONE_WORDS = ("完成", "做完", "搞定", "done", "标记", "已办")
HELP_WORDS = ("帮助", "help", "用法", "指令", "怎么用")
TODAY_WORDS = ("今日", "今天", "本日")
WEEK_WORDS = ("本周", "这周", "一周", "7天", "七天")


def parse(text):
    t = text.strip()
    low = t.lower()

    if any(w in low for w in HELP_WORDS) and len(t) <= 6:
        return {"cmd": "help"}

    # completion: explicit id preferred
    if any(w in low for w in DONE_WORDS):
        m = re.search(r"(\d+)", t)
        if m:
            return {"cmd": "done", "id": int(m.group(1))}
        keyword = t
        for w in DONE_WORDS:
            keyword = keyword.replace(w, " ")
        keyword = keyword.strip(" ：:，,。.!！好的我了下")
        if keyword:
            return {"cmd": "done", "keyword": keyword}
        return {"cmd": "done", "keyword": ""}

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
    if due:
        parts.append(due)
    if with_id:
        parts.append(f"(#{task['id']})")
    return " ".join(parts)


def format_ack(priority, title, due):
    """Format the mandatory acknowledgement: [P几],事件名,时间,已被记录"""
    bits = [f"[{priority}]", title]
    if due:
        bits.append(fmt_due(due))
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
    "· 查询：查询 / 今日 / 本周 / P0 / 待办\n"
    "· 完成：完成 3（编号）或 完成了高数作业\n"
    "· 帮助：帮助\n\n"
    "优先级：P0=48小时内紧急  P1=作业/有截止  P2=重要不紧急  P3=备忘"
)