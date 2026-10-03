"""QQ Bot (official v2 API) client: access token, WebSocket gateway, C2C messages.

Only the C2C (single chat) scenario is implemented, which is what the personal
todo assistant needs. Requires the GROUP_AND_C2C_EVENT intent permission.
"""

import asyncio
import json
import logging
import time

import httpx
import websockets

log = logging.getLogger("qqbot")

# OpCodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class TokenManager:
    def __init__(self, app_id, app_secret, token_url):
        self.app_id = app_id
        self.app_secret = app_secret
        self.token_url = token_url
        self._token = None
        self._expires_at = 0.0

    async def get(self):
        if self._token and time.time() < self._expires_at:
            return self._token
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                self.token_url,
                json={"appId": self.app_id, "clientSecret": self.app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"get access_token failed: {data}")
        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 7200))
        self._expires_at = time.time() + max(expires_in - 120, 60)
        log.info("access_token refreshed, expires_in=%ss", expires_in)
        return self._token


class QQBot:
    def __init__(self, config, on_c2c):
        qq = config["qq"]
        self.api_base = qq["api_base"].rstrip("/")
        self.intents = int(qq.get("intents", 33554432))
        self.token = TokenManager(qq["app_id"], qq["app_secret"], qq["token_url"])
        self.on_c2c = on_c2c  # async def (openid, content, msg_id)
        self.session_id = None
        self.last_seq = None
        self._msg_seq = {}
        self._ws = None

    async def _headers(self):
        return {
            "Authorization": f"QQBot {await self.token.get()}",
            "Content-Type": "application/json",
        }

    async def _get_gateway(self):
        async with httpx.AsyncClient(timeout=20) as client:
            for path in ("/gateway", "/websocket"):
                try:
                    resp = await client.get(
                        self.api_base + path, headers=await self._headers()
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        url = data.get("url") or data.get("wss")
                        if url:
                            return url
                except httpx.HTTPError as exc:
                    log.warning("gateway %s failed: %s", path, exc)
        raise RuntimeError("unable to fetch gateway url")

    async def send_c2c(self, openid, content, msg_id=None):
        """Send a message in single chat. If msg_id is given this is a passive
        reply; otherwise it is a proactive message."""
        seq = 1
        if msg_id:
            seq = self._msg_seq.get(msg_id, 0) + 1
            self._msg_seq[msg_id] = seq
            if len(self._msg_seq) > 500:
                self._msg_seq.clear()
                self._msg_seq[msg_id] = seq
        body = {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        url = f"{self.api_base}/v2/users/{openid}/messages"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=await self._headers(), json=body)
        if resp.status_code >= 300:
            log.error("send_c2c failed %s: %s", resp.status_code, resp.text)
        return resp

    async def run(self):
        backoff = 3
        while True:
            try:
                gateway = await self._get_gateway()
                async with websockets.connect(gateway, max_size=2**22) as ws:
                    self._ws = ws
                    backoff = 3
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("gateway connection error: %s (retry in %ss)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _session(self, ws):
        heartbeat_task = None
        try:
            async for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if op == OP_HELLO:
                    interval = msg["d"]["heartbeat_interval"] / 1000
                    if self.session_id and self.last_seq is not None:
                        await self._resume(ws)
                    else:
                        await self._identify(ws)
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(ws, interval)
                    )
                elif op == OP_DISPATCH:
                    if msg.get("s") is not None:
                        self.last_seq = msg["s"]
                    await self._dispatch(msg)
                elif op == OP_HEARTBEAT_ACK:
                    log.debug("heartbeat ack")
                elif op == OP_RECONNECT:
                    log.info("server asked to reconnect")
                    return
                elif op == OP_INVALID_SESSION:
                    log.warning("invalid session; will re-identify")
                    self.session_id = None
                    self.last_seq = None
                    return
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()

    async def _identify(self, ws):
        await ws.send(
            json.dumps(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {await self.token.get()}",
                        "intents": self.intents,
                        "shard": [0, 1],
                        "properties": {},
                    },
                }
            )
        )

    async def _resume(self, ws):
        log.info("resuming session %s seq=%s", self.session_id, self.last_seq)
        await ws.send(
            json.dumps(
                {
                    "op": OP_RESUME,
                    "d": {
                        "token": f"QQBot {await self.token.get()}",
                        "session_id": self.session_id,
                        "seq": self.last_seq,
                    },
                }
            )
        )

    async def _heartbeat_loop(self, ws, interval):
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self.last_seq}))
            except Exception:  # noqa: BLE001
                return

    async def _dispatch(self, msg):
        t = msg.get("t")
        d = msg.get("d") or {}
        if t == "READY":
            self.session_id = d.get("session_id")
            log.info("READY session=%s", self.session_id)
        elif t == "RESUMED":
            log.info("RESUMED")
        elif t == "C2C_MESSAGE_CREATE":
            author = d.get("author") or {}
            openid = author.get("user_openid") or author.get("id")
            content = (d.get("content") or "").strip()
            msg_id = d.get("id")
            log.info("C2C message from %s: %s", openid, content)
            if openid and content:
                asyncio.create_task(self.on_c2c(openid, content, msg_id))