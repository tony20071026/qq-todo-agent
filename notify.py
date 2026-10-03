"""ntfy push notifications."""

import logging

import httpx

log = logging.getLogger("notify")

PRIORITY_MAP = {"P0": "max", "P1": "high", "P2": "default", "P3": "low"}
TAG_MAP = {"P0": "rotating_light", "P1": "memo", "P2": "pushpin", "P3": "paperclip"}


class Ntfy:
    def __init__(self, server, topic, token=""):
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token

    async def send(self, message, title="待办提醒", priority="default", tags=None):
        headers = {"Title": _encode_header(title),
                   "Priority": priority}
        if tags:
            headers["Tags"] = tags
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.server}/{self.topic}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, content=message.encode("utf-8"),
                                         headers=headers)
                if resp.status_code >= 300:
                    log.error("ntfy send failed %s: %s", resp.status_code, resp.text)
                return resp.status_code < 300
        except httpx.HTTPError as exc:
            log.error("ntfy send error: %s", exc)
            return False

    async def notify_task(self, task, kind="提醒"):
        p = task.get("priority", "P3")
        title = f"{kind} [{p}] {task['title']}"
        lines = [f"优先级：{p}", f"事项：{task['title']}"]
        if task.get("due_at"):
            lines.append(f"截止：{task['due_at']}")
        lines.append(f"编号：{task['id']}")
        return await self.send(
            "\n".join(lines),
            title=title,
            priority=PRIORITY_MAP.get(p, "default"),
            tags=TAG_MAP.get(p),
        )


def _encode_header(value):
    """ntfy headers should be ASCII; encode non-ASCII via RFC 2047."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        from email.header import Header

        return Header(value, "utf-8").encode()