"""QQ todo assistant - entrypoint.

Receives C2C messages from QQ, classifies them with an LLM, stores them in
memo.db, and pushes daily digests / event reminders through ntfy.
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

import commands
import db
from llm import LLM
from notify import Ntfy
from qqbot import QQBot
from scheduler import Scheduler

log = logging.getLogger("main")


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _filter_range(tasks, rng):
    today = datetime.now().astimezone().date()
    out = []
    for t in tasks:
        if not t.get("due_at"):
            continue
        try:
            d = datetime.fromisoformat(t["due_at"]).date()
        except ValueError:
            continue
        if rng == "today" and d <= today:
            out.append(t)
        elif rng == "week" and d <= today + timedelta(days=7):
            out.append(t)
    return out


async def handle_set_digest(bot, db_path, openid, msg_id, times, default_times, tz):
    """Query or update the daily broadcast times (stored as a DB override)."""
    if not times:
        raw = db.kv_get(db_path, "digest_times")
        current = default_times
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and parsed:
                    current = sorted(parsed)
            except ValueError:
                pass
        await bot.send_c2c(
            openid,
            "当前每日播报时间：" + "、".join(current) + "（北京时间）。"
            "发送「播报时间改成 08:30 和 23:30」即可调整。",
            msg_id,
        )
        return
    times = sorted(set(times))
    db.kv_set(db_path, "digest_times", json.dumps(times))
    # Mark already-passed slots today as sent so we don't fire a catch-up now.
    now = datetime.now(ZoneInfo(tz))
    today = now.strftime("%Y-%m-%d")
    try:
        sent = json.loads(db.kv_get(db_path, "last_digest") or "{}")
    except ValueError:
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    for slot in times:
        hh, mm = (slot.split(":") + ["0"])[:2]
        target = now.replace(hour=int(hh), minute=int(mm), second=0,
                             microsecond=0)
        if now >= target:
            sent[slot] = today
    db.kv_set(db_path, "last_digest", json.dumps(sent))
    await bot.send_c2c(
        openid,
        "已把每日播报时间改为：" + "、".join(times) + "（北京时间）。",
        msg_id,
    )


async def handle_trash(bot, db_path, openid, msg_id):
    tasks = db.list_tasks(db_path, status="trashed")
    if not tasks:
        await bot.send_c2c(openid, "🗑️ 垃圾箱是空的。", msg_id)
        return
    await bot.send_c2c(openid, commands.format_query(tasks, "🗑️ 垃圾箱"), msg_id)


async def handle_empty_trash(bot, db_path, openid, msg_id):
    n = db.delete_trashed(db_path)
    if n:
        await bot.send_c2c(openid, f"🧹 已清空垃圾箱，删除 {n} 项。", msg_id)
    else:
        await bot.send_c2c(openid, "垃圾箱已经是空的。", msg_id)


async def handle_delete(bot, db_path, openid, msg_id, ids):
    if not ids:
        await bot.send_c2c(openid, "请指定要清除的编号，如：清除 3。", msg_id)
        return
    trashed = {t["id"]: t for t in db.list_tasks(db_path, status="trashed")}
    removed = [trashed[i]["title"] for i in ids if i in trashed]
    missing = [i for i in ids if i not in trashed]
    n = db.delete_tasks(db_path, ids)
    if removed:
        await bot.send_c2c(
            openid, f"🧹 已从垃圾箱清除 {n} 项：{'、'.join(removed)}", msg_id)
    if missing and not removed:
        await bot.send_c2c(
            openid, f"垃圾箱里没有：{'、'.join('#' + str(i) for i in missing)}", msg_id)


async def handle_restore(bot, db_path, openid, msg_id, task_id=None, keyword=None):
    target = str(task_id) if task_id is not None else keyword
    matches = _find_matches(db_path, target, status="trashed")
    if not matches:
        await bot.send_c2c(openid, f"垃圾箱里没有找到「{target}」。", msg_id)
        return
    if len(matches) > 1:
        await bot.send_c2c(
            openid,
            commands.format_query(matches, "匹配到多个，请回复要恢复的编号"),
            msg_id,
        )
        return
    task = matches[0]
    db.update_task(db_path, task["id"], status="pending", trash_exempt=1)
    await bot.send_c2c(
        openid,
        f"已恢复 [{task['priority']}] {task['title']} (#{task['id']})，"
        f"可用「修改 {task['id']} <新时间>」重新设置截止时间。",
        msg_id,
    )


async def handle_query(bot, db_path, openid, msg_id, priority=None, rng=None):
    tasks = db.list_tasks(db_path, status="pending", priority=priority)
    if rng:
        tasks = _filter_range(tasks, rng)
    title = "待办"
    if priority:
        title = f"{priority} 待办"
    if rng == "today":
        title = "今日待办"
    elif rng == "week":
        title = "本周待办"
    await bot.send_c2c(openid, commands.format_query(tasks, title), msg_id)


def _find_matches(db_path, target, status="pending"):
    """Resolve a task by explicit id, then by title, then by raw text."""
    if not target:
        return []
    explicit = _explicit_id(target)
    if explicit is not None:
        task = db.get_task(db_path, explicit)
        if task and task.get("status") == status:
            return [task]
        return []
    matches = db.find_tasks_by_keyword(db_path, target, status=status,
                                       title_only=True)
    if not matches:
        matches = db.find_tasks_by_keyword(db_path, target, status=status)
    return matches


def _recent_context(db_path, limit=8):
    tasks = db.list_tasks(db_path, status="pending")
    tasks.sort(key=lambda t: t["id"], reverse=True)
    lines = [f"#{t['id']} [{t['priority']}] {t['title']}" for t in tasks[:limit]]
    last = db.kv_get(db_path, "last_task_id")
    if last:
        lines.append(f"（最近新增的待办编号是 #{last}）")
    return lines


async def handle_done(bot, db_path, openid, msg_id, target=None, task_id=None):
    if task_id is not None:
        matches = _find_matches(db_path, str(task_id))
    else:
        matches = _find_matches(db_path, target)
    if not matches:
        await bot.send_c2c(openid, f"没有找到匹配「{target or task_id}」的待办。", msg_id)
        return
    if len(matches) > 1:
        listing = commands.format_query(matches, "匹配到多个，请回复要完成的编号")
        await bot.send_c2c(openid, listing, msg_id)
        return
    task = matches[0]
    db.update_status(db_path, task["id"], "done")
    await bot.send_c2c(
        openid,
        f"已完成 [{task['priority']}] {task['title']} (#{task['id']})，干得漂亮！",
        msg_id,
    )


async def handle_update(bot, db_path, openid, msg_id, target, new_title,
                        new_due, new_priority):
    if not target:
        target = db.kv_get(db_path, "last_task_id")
    matches = _find_matches(db_path, target)
    if not matches and target:
        # maybe they referred to the most recently added task
        last = db.kv_get(db_path, "last_task_id")
        if last and last != target:
            matches = _find_matches(db_path, last)
    if not matches:
        await bot.send_c2c(openid, f"没有找到要修改的待办「{target}」。", msg_id)
        return
    if len(matches) > 1:
        listing = commands.format_query(matches, "匹配到多个，请回复要修改的编号")
        await bot.send_c2c(openid, listing, msg_id)
        return
    task = matches[0]
    fields, changes = {}, []
    if new_title:
        fields["title"] = new_title
        changes.append(f"标题→{new_title}")
    if new_due:
        fields["due_at"] = new_due
        changes.append(f"时间→{commands.fmt_due(new_due)}")
    if new_priority:
        fields["priority"] = new_priority
        changes.append(f"优先级→{new_priority}")
    if not fields:
        await bot.send_c2c(
            openid, f"要把 #{task['id']}「{task['title']}」改成什么？", msg_id)
        return
    db.update_task(db_path, task["id"], **fields)
    updated = db.get_task(db_path, task["id"])
    await bot.send_c2c(
        openid,
        f"已更新 #{updated['id']}「{updated['title']}」：{'，'.join(changes)}",
        msg_id,
    )


async def handle_message(config, bot, llm, db_path, openid, content, msg_id):
    if not db.mark_processed(db_path, msg_id):
        return
    db.kv_set(db_path, "last_openid", openid)

    cmd = commands.parse(content)
    if cmd:
        if cmd["cmd"] == "help":
            await bot.send_c2c(openid, commands.HELP_TEXT, msg_id)
        elif cmd["cmd"] == "query":
            await handle_query(bot, db_path, openid, msg_id,
                               cmd.get("priority"), cmd.get("range"))
        elif cmd["cmd"] == "trash":
            await handle_trash(bot, db_path, openid, msg_id)
        elif cmd["cmd"] == "restore":
            await handle_restore(bot, db_path, openid, msg_id,
                                 cmd.get("id"), cmd.get("keyword"))
        elif cmd["cmd"] == "empty_trash":
            await handle_empty_trash(bot, db_path, openid, msg_id)
        elif cmd["cmd"] == "delete":
            await handle_delete(bot, db_path, openid, msg_id, cmd.get("ids"))
        return

    try:
        res = await llm.analyze(content, context=_recent_context(db_path))
    except Exception as exc:  # noqa: BLE001
        log.exception("LLM analyze failed: %s", exc)
        await bot.send_c2c(openid, "抱歉，我暂时无法理解这条消息，请稍后重试。", msg_id)
        return

    intent = res["intent"]
    if intent == "add":
        tasks = res.get("tasks") or []
        if not tasks:
            tasks = [{"priority": res["priority"] or "P3",
                      "title": res["title"] or content,
                      "due_time": res["due_time"], "repeat": None}]
        acks = []
        last_id = None
        for t in tasks:
            repeat = t.get("repeat")
            priority = t["priority"] if repeat else db.effective_priority(
                t["priority"], t["due_time"])
            last_id = db.add_task(db_path, t["title"], priority, t["due_time"],
                                  source_msg_id=msg_id, raw_text=content,
                                  recur=repeat)
            acks.append(commands.format_ack(priority, t["title"], t["due_time"],
                                            repeat=repeat))
        if last_id:
            db.kv_set(db_path, "last_task_id", last_id)
        await bot.send_c2c(openid, "\n".join(acks), msg_id)
    elif intent == "done":
        target = res.get("target") or res.get("title")
        explicit = _explicit_id(target)
        if explicit is None:
            explicit = _explicit_id(content)
        if explicit is not None:
            await handle_done(bot, db_path, openid, msg_id, task_id=explicit)
        else:
            await handle_done(bot, db_path, openid, msg_id, target=target or content)
    elif intent == "update":
        await handle_update(bot, db_path, openid, msg_id, res.get("target"),
                            res.get("new_title"), res.get("new_due"),
                            res.get("new_priority"))
    elif intent == "set_digest":
        app_cfg = config.get("app", {})
        default_times = app_cfg.get("digest_times") or [
            app_cfg.get("digest_time", "08:00")]
        await handle_set_digest(bot, db_path, openid, msg_id,
                                res.get("digest_times"), default_times,
                                app_cfg.get("timezone", "Asia/Shanghai"))
    elif intent == "query":
        await handle_query(bot, db_path, openid, msg_id)
    else:  # help / chat
        explicit = _explicit_id(content)
        if explicit is not None and db.get_task(db_path, explicit):
            # user replied with a task number after being asked which one
            await handle_done(bot, db_path, openid, msg_id, task_id=explicit)
        else:
            await bot.send_c2c(openid, res.get("reply") or commands.HELP_TEXT, msg_id)


def _explicit_id(text):
    """Return the task id for inputs like '2', '#2', '第2条', else None."""
    m = re.fullmatch(r"\s*#?\s*第?\s*(\d+)\s*[条号]?\s*", text or "")
    return int(m.group(1)) if m else None


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = os.environ.get("QQ_AGENT_CONFIG", "config.yaml")
    if not os.path.exists(config_path):
        log.error("config file not found: %s (copy config.example.yaml)", config_path)
        sys.exit(1)
    config = load_config(config_path)

    db_path = config["app"]["db_path"]
    db.init_db(db_path)

    llm = LLM(
        config["llm"]["base_url"],
        config["llm"]["api_key"],
        config["llm"]["model"],
        config["llm"].get("timeout", 60),
        config["app"].get("timezone", "Asia/Shanghai"),
    )
    ntfy = Ntfy(
        config["ntfy"]["server"],
        config["ntfy"]["topic"],
        config["ntfy"].get("token", ""),
    )
    bot = QQBot(config, lambda o, c, m: handle_message(
        config, bot, llm, db_path, o, c, m))

    scheduler = Scheduler(config, llm, ntfy)
    log.info("starting QQ todo assistant")
    await asyncio.gather(bot.run(), scheduler.run())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass