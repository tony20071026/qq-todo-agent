"""LLM helpers: intent detection, task classification, daily summary.

Uses the Alibaba Cloud Model Studio OpenAI-compatible /chat/completions API.
"""

import json
import re
from datetime import datetime

import httpx

PRIORITIES = ("P0", "P1", "P2", "P3")

SYSTEM_PROMPT = """你是一个个人待办助理，负责理解用户用中文发来的消息。
你必须只输出一个 JSON 对象，不要输出任何解释或 Markdown 代码块。

JSON 字段：
- intent: "add" | "done" | "query" | "help" | "chat"
- priority: "P0" | "P1" | "P2" | "P3"（intent 为 add/done 时必填，否则可为 null）
- title: 简洁的事件名（intent 为 add 时必填；done 时为要完成的事项关键词）
- due_time: 截止时间，ISO8601 带时区偏移，如 "2026-10-05T18:00:00+08:00"；无法确定则为 null
- reply: 当 intent 为 chat/help 时给用户的简短中文回复，否则为 null

优先级判定规则：
- P0：48 小时内必须完成的紧急任务（截止时间在 48 小时内，或用户明确表示很紧急）
- P1：课程作业、有明确截止时间的学业/工作任务
- P2：重要但不紧急（长期项目、考试/面试准备、健康、财务等）
- P3：信息记录、低优先级、可延后的事项

时间解析规则（当前时区 Asia/Shanghai）：
- “明天下午3点”“周五交”“下周一”等相对时间，基于下方提供的当前时间换算。
- 只有日期没有具体时间时，使用该日期 23:59:59。
- 完全无法推断时间时，due_time 为 null，不要编造。

意图判定：
- 记录新事项 -> add
- 表示某事项已完成 -> done
- 询问待办/查询 -> query
- 询问用法 -> help
- 其他闲聊 -> chat
"""


class LLM:
    def __init__(self, base_url, api_key, model, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

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

    async def analyze(self, raw_text, now=None):
        now = now or datetime.now().astimezone()
        user_msg = (
            f"当前时间：{now.isoformat(timespec='seconds')}\n"
            f"用户消息：{raw_text}"
        )
        content = await self._chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ]
        )
        result = self._extract_json(content)
        return self._normalize(result)

    @staticmethod
    def _normalize(result):
        out = {
            "intent": result.get("intent") or "add",
            "priority": result.get("priority"),
            "title": (result.get("title") or "").strip() or None,
            "due_time": result.get("due_time") or None,
            "reply": result.get("reply") or None,
        }
        if out["intent"] in ("add", "done"):
            if out["priority"] not in PRIORITIES:
                out["priority"] = "P3"
        else:
            out["priority"] = None
        if out["due_time"] and len(out["due_time"]) == 10:
            out["due_time"] = out["due_time"] + "T23:59:59+08:00"
        return out

    async def summarize(self, tasks, now=None):
        """Generate a natural-language daily digest from pending tasks."""
        now = now or datetime.now().astimezone()
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