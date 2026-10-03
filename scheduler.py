"""Background scheduler: daily digest + per-event reminders (asyncio loop)."""

import asyncio
import logging
from datetime import datetime, timedelta

import db

log = logging.getLogger("scheduler")


class Scheduler:
    def __init__(self, config, llm, ntfy):
        app = config["app"]
        self.db_path = app["db_path"]
        self.tz = app.get("timezone", "Asia/Shanghai")
        self.digest_time = app.get("digest_time", "08:00")
        self.reminders = {k: list(v) for k, v in (app.get("reminders") or {}).items()}
        self.tick = int(app.get("tick", 30))
        self.llm = llm
        self.ntfy = ntfy

    async def run(self):
        log.info("scheduler started (digest=%s tz=%s)", self.digest_time, self.tz)
        while True:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("scheduler tick error: %s", exc)
            await asyncio.sleep(self.tick)

    async def tick_once(self):
        now = datetime.now().astimezone()
        await self._check_reminders(now)
        await self._check_digest(now)

    async def _check_reminders(self, now):
        tasks = db.list_tasks(self.db_path, status="pending")
        for t in tasks:
            if not t.get("due_at"):
                continue
            try:
                due = datetime.fromisoformat(t["due_at"])
            except ValueError:
                continue
            leads = self.reminders.get(t["priority"], [])
            if not leads:
                continue
            reminded = set(db.get_reminded(t))
            applicable = [
                lead
                for lead in leads
                if lead not in reminded and now >= due - timedelta(minutes=lead)
            ]
            if not applicable:
                continue
            chosen = min(applicable)
            if now > due:
                kind = "已逾期"
            elif chosen <= 120:
                kind = "即将截止"
            elif chosen <= 1440:
                kind = "明日截止"
            else:
                kind = "提醒"
            log.info("reminder task #%s (%s)", t["id"], kind)
            await self.ntfy.notify_task(t, kind=kind)
            reminded.update(applicable)
            db.set_reminded(self.db_path, t["id"], sorted(reminded))

    async def _check_digest(self, now):
        today = now.strftime("%Y-%m-%d")
        if db.kv_get(self.db_path, "last_digest_date") == today:
            return
        hh, mm = (self.digest_time.split(":") + ["0"])[:2]
        target = now.replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0
        )
        if now < target:
            return
        tasks = db.list_tasks(self.db_path, status="pending")
        summary = await self.llm.summarize(tasks)
        body = summary
        if tasks:
            body += "\n\n— 明细 —\n"
            for t in tasks:
                due = (" 截止 " + t["due_at"][:16].replace("T", " ")) if t.get("due_at") else ""
                body += f"[{t['priority']}] {t['title']}{due} (#{t['id']})\n"
        log.info("sending daily digest (%d tasks)", len(tasks))
        await self.ntfy.send(body, title="每日待办汇总", priority="default",
                             tags="calendar")
        db.kv_set(self.db_path, "last_digest_date", today)