"""Background scheduler: daily digest + per-event reminders (asyncio loop)."""

import asyncio
import logging
from datetime import datetime, timedelta

import db

log = logging.getLogger("scheduler")


def _add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                       else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return dt.replace(year=year, month=month, day=day)


def next_occurrence(due, rule, now):
    """Advance a recurring due time to the next occurrence strictly after now."""
    freq = rule.get("freq", "daily")
    interval = max(int(rule.get("interval", 1) or 1), 1)
    steps = 0
    while due <= now and steps < 100000:
        if freq == "weekly":
            due = due + timedelta(weeks=interval)
        elif freq == "monthly":
            due = _add_months(due, interval)
        else:
            due = due + timedelta(days=interval)
        steps += 1
    return due


class Scheduler:
    def __init__(self, config, llm, ntfy):
        app = config["app"]
        self.db_path = app["db_path"]
        self.tz = app.get("timezone", "Asia/Shanghai")
        self.digest_time = app.get("digest_time", "08:00")
        self.reminders = {k: list(v) for k, v in (app.get("reminders") or {}).items()}
        self.tick = int(app.get("tick", 30))
        self.trash_after = float(app.get("trash_after_hours", 3))
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
        self._upgrade_priorities(now)
        self._trash_expired(now)
        await self._check_reminders(now)
        await self._check_digest(now)

    def _trash_expired(self, now):
        """Move one-off tasks that are overdue by trash_after hours to trash."""
        cutoff = timedelta(hours=self.trash_after)
        for t in db.list_tasks(self.db_path, status="pending"):
            if t.get("recur") or not t.get("due_at"):
                continue
            if t.get("trash_exempt"):
                continue
            try:
                due = datetime.fromisoformat(t["due_at"])
            except ValueError:
                continue
            if now >= due + cutoff:
                db.update_status(self.db_path, t["id"], "trashed")
                log.info("task #%s trashed (overdue %s)", t["id"], t["due_at"])

    def _upgrade_priorities(self, now):
        """P1 tasks entering the 48h window are promoted to P0 (one-off only)."""
        for t in db.list_tasks(self.db_path, status="pending", priority="P1"):
            if t.get("recur"):
                continue
            if db.effective_priority(t["priority"], t["due_at"], now) == "P0":
                db.set_priority(self.db_path, t["id"], "P0")
                log.info("task #%s upgraded P1 -> P0 (due %s)", t["id"], t["due_at"])

    async def _check_reminders(self, now):
        tasks = db.list_tasks(self.db_path, status="pending")
        for t in tasks:
            if not t.get("due_at"):
                continue
            try:
                due = datetime.fromisoformat(t["due_at"])
            except ValueError:
                continue
            if t.get("recur"):
                await self._check_recurring(t, due, now)
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

    async def _check_recurring(self, t, due, now):
        """Recurring tasks notify at the occurrence time, then roll forward."""
        if now < due:
            return
        rule = db.get_recur(t) or {"freq": "daily", "interval": 1}
        log.info("recurring reminder task #%s (%s)", t["id"], t["title"])
        await self.ntfy.notify_task(t, kind="到点提醒")
        nxt = next_occurrence(due, rule, now)
        until = rule.get("until")
        if until:
            try:
                if nxt > datetime.fromisoformat(until):
                    db.update_status(self.db_path, t["id"], "done")
                    log.info("recurring task #%s finished", t["id"])
                    return
            except ValueError:
                pass
        db.update_task(self.db_path, t["id"],
                       due_at=nxt.isoformat(timespec="seconds"), reminded="[]")

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