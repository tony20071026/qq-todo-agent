"""QQ todo assistant - entrypoint.

Receives C2C messages from QQ, classifies them with an LLM, stores them in
memo.db, and pushes daily digests / event reminders through ntfy.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

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


async def handle_done(bot, db_path, openid, msg_id, task_id=None, keyword=None):
    if task_id is not None:
        task = db.get_task(db_path, task_id)
        if not task:
            await bot.send_c2c(openid, f"没有找到编号 #{task_id} 的待办。", msg_id)
            return
        matches = [task]
    else:
        matches = db.find_tasks_by_keyword(db_path, keyword or "") if keyword else []
    if not matches:
        await bot.send_c2c(openid, f"没有找到匹配「{keyword or task_id}」的待办。", msg_id)
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
        elif cmd["cmd"] == "done":
            await handle_done(bot, db_path, openid, msg_id,
                              cmd.get("id"), cmd.get("keyword"))
        return

    try:
        res = await llm.analyze(content)
    except Exception as exc:  # noqa: BLE001
        log.exception("LLM analyze failed: %s", exc)
        await bot.send_c2c(openid, "抱歉，我暂时无法理解这条消息，请稍后重试。", msg_id)
        return

    intent = res["intent"]
    if intent == "add":
        title = res["title"] or content
        due = res["due_time"]
        db.add_task(db_path, title, res["priority"], due,
                    source_msg_id=msg_id, raw_text=content)
        ack = commands.format_ack(res["priority"], title, due)
        await bot.send_c2c(openid, ack, msg_id)
    elif intent == "done":
        await handle_done(bot, db_path, openid, msg_id,
                          keyword=res.get("title") or content)
    elif intent == "query":
        await handle_query(bot, db_path, openid, msg_id)
    else:  # help / chat
        await bot.send_c2c(openid, res.get("reply") or commands.HELP_TEXT, msg_id)


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