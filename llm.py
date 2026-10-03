"""LLM helpers: intent detection, task classification, daily summary.

Uses the Alibaba Cloud Model Studio OpenAI-compatible /chat/completions API.
"""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

PRIORITIES = ("P0", "P1", "P2", "P3")

SYSTEM_PROMPT = """你是一个个人待办助理，负责理解用户用中文发来的消息（可能是一条转发通知或一长段作业清单）。
你必须只输出一个 JSON 对象，不要输出任何解释或 Markdown 代码块。

JSON 字段：
- intent: "add" | "done" | "update" | "query" | "help" | "set_digest" | "chat"
- tasks: 当 intent 为 "add" 时，把消息拆解为一条或多条待办，数组，每项字段：
    - priority: "P0" | "P1" | "P2" | "P3"
    - title: 简洁的事件名（20 字以内）
    - due_time: 首次触发时间，ISO8601 带时区偏移，如 "2026-10-04T23:00:00+08:00"；无法确定则为 null
    - repeat: 周期规则。一次性任务为 null；重复任务填对象：
        {"freq": "daily" | "weekly" | "monthly", "interval": 数字, "until": "ISO8601 或 null"}
      freq 为频率，interval 为间隔（每隔几个周期），until 为重复结束时间（含当天，无法确定则 null）。
      例：“往后30天每天晚上11点” -> due_time 为今晚/明晚 23:00，repeat 为
      {"freq":"daily","interval":1,"until":"<距今30天后的日期>T23:00:00+08:00"}。
      若消息只包含一件事，也要返回长度为 1 的数组。
- target: 当 intent 为 "done" 或 "update" 时，要操作的事项，填编号（如 "3"）或关键词；无法确定填 null
- new_title / new_due / new_priority: 当 intent 为 "update" 时，用户要改成的新值，未提及的填 null
- digest_times: 当 intent 为 "set_digest" 时，用户希望设置的每日播报时间列表，
  24 小时制字符串数组，如 ["08:30","23:30"]（北京时间）；若用户只是询问当前播报时间则为 null
- reply: 当 intent 为 "chat"/"help" 时给用户的简短中文回复，否则为 null

优先级判定规则：
- P0：48 小时内必须完成的紧急任务（截止时间在 48 小时内，或用户明确表示很紧急）
- P1：课程作业、有明确截止时间的学业/工作任务
- P2：重要但不紧急（长期项目、考试/面试准备、健康、财务等）
- P3：信息记录、低优先级、可延后的事项

时间解析规则（当前时区 Asia/Shanghai）：
- “明天下午3点”“周五交”“下周一”“10月9日21:00”等时间，基于下方提供的当前时间换算。
- 只有日期没有具体时间时，使用该日期 23:59:59。
- 完全无法推断时间时，due_time 为 null，不要编造。

意图判定：
- 记录新事项（含转发的作业/通知） -> add
- 表示某事项已完成 -> done
- 用户要修改已有事项（改时间/标题/优先级） -> update
- 用户要调整每日播报/推送的时间（如“改成早上9点和晚上10点播报”） -> set_digest
- 询问待办/查询 -> query
- 询问用法 -> help
- 其他闲聊 -> chat

若用户在纠正/修改刚说过的事项（如“整错了”“改成”“不是X是Y”），用下方“最近待办”列表中最相关的一项作为 target。
"""


class LLM:
    def __init__(self, base_url, api_key, model, timeout=60, tz="Asia/Shanghai"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.zone = ZoneInfo(tz)

    async def _chat(self, messages, temperature=0.2):
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    @staticmethod
    def _extract_json(text):
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        try:
            return json.loads(text)
        except ValueError:
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                return json.loads(match.group(0))
            raise

    async def analyze(self, raw_text, now=None, context=None):
        now = now or datetime.now(self.zone)
        parts = [f"当前时间：{now.isoformat(timespec='seconds')}"]
        if context:
            parts.append("最近待办：\n" + "\n".join(context))
        parts.append(f"用户消息：{raw_text}")
        content = await self._chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(parts)},
            ]
        )
        result = self._extract_json(content)
        return self._normalize(result)

    @staticmethod
    def _norm_due(due):
        if due and len(due) == 10:
            return due + "T23:59:59+08:00"
        return due or None

    @staticmethod
    def _norm_time(value):
        if not isinstance(value, str):
            return None
        m = re.fullmatch(r"\s*(\d{1,2})\s*[:：点时]\s*(\d{1,2})?\s*(?:分)?\s*", value)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return None

    @staticmethod
    def _norm_recur(recur):
        if not isinstance(recur, dict):
            return None
        freq = recur.get("freq")
        if freq not in ("daily", "weekly", "monthly"):
            return None
        try:
            interval = int(recur.get("interval") or 1)
        except (ValueError, TypeError):
            interval = 1
        interval = max(interval, 1)
        until = recur.get("until")
        if until and len(until) == 10:
            until = until + "T23:59:59+08:00"
        return {"freq": freq, "interval": interval, "until": until}

    @classmethod
    def _normalize(cls, result):
        intent = result.get("intent") or "add"
        out = {
            "intent": intent,
            "reply": result.get("reply") or None,
            "target": (result.get("target") or "").strip() or None,
            "new_title": (result.get("new_title") or "").strip() or None,
            "new_due": cls._norm_due(result.get("new_due")),
            "new_priority": result.get("new_priority")
            if result.get("new_priority") in PRIORITIES else None,
            "digest_times": None,
            "priority": None,
            "title": None,
            "due_time": None,
            "tasks": [],
        }
        if intent == "add":
            raw = result.get("tasks")
            if not isinstance(raw, list) or not raw:
                raw = [{
                    "priority": result.get("priority"),
                    "title": result.get("title"),
                    "due_time": result.get("due_time"),
                }]
            for item in raw:
                if not isinstance(item, dict):
                    continue
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                priority = item.get("priority")
                if priority not in PRIORITIES:
                    priority = "P3"
                out["tasks"].append({
                    "priority": priority,
                    "title": title,
                    "due_time": cls._norm_due(item.get("due_time")),
                    "repeat": cls._norm_recur(item.get("repeat")),
                })
        elif intent in ("done", "update"):
            priority = result.get("priority")
            out["priority"] = priority if priority in PRIORITIES else "P3"
            # keep legacy "title" as the done target keyword
            out["title"] = (result.get("title") or "").strip() or None
            out["due_time"] = cls._norm_due(result.get("due_time"))
        elif intent == "set_digest":
            raw = result.get("digest_times")
            if isinstance(raw, list):
                times = []
                for item in raw:
                    norm = cls._norm_time(item)
                    if norm and norm not in times:
                        times.append(norm)
                out["digest_times"] = sorted(times) or None
        return out

    async def summarize(self, tasks, now=None):
        """Generate a natural-language daily digest from pending tasks."""
        now = now or datetime.now(self.zone)
        lines = []
        for t in tasks:
            due = t.get("due_at") or "无截止"
            lines.append(f"- [{t['priority']}] {t['title']} (截止: {due}, id={t['id']})")
        if not lines:
            return "今天没有待办事项，放松一下吧。"
        task_text = "\n".join(lines)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是个人待办助理。请把待办列表总结成一段简洁、友好的中文每日提醒，"
                    "按优先级 P0>P1>P2>P3 排序，突出今天及 48 小时内到期的紧急事项。"
                    "只输出纯文本，不要 Markdown 表格。"
                ),
            },
            {
                "role": "user",
                "content": f"当前时间：{now.isoformat(timespec='seconds')}\n待办：\n{task_text}",
            },
        ]
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            url = f"{self.base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.5,
            }
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()