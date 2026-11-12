"""Connection to the OpsLab Bot Hub (developer site): registration, heartbeats, updates, feature requests.

Updates are carried out on the host by updater.sh (installed by install.sh as a cron job): the panel only drops
data/update-request.json and reads data/update-status.json.
"""
import os, json, uuid, asyncio, logging, datetime as dt
import aiohttp

import core

log = logging.getLogger("economy.hub")
HUB_URL = os.getenv("HUB_URL", "https://ops-discordb.opslabsystems.cloud").rstrip("/")
BASE = os.path.dirname(os.path.abspath(__file__))
INTERVAL = 15 * 60


def version() -> str:
    try:
        with open(os.path.join(BASE, "VERSION")) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "0.0.0"


def vtuple(v):
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4]) or (0,)


class Hub:
    def __init__(self, state, data_dir):
        self.state = state
        self.data = data_dir
        self.latest = None
        self.update_available = False
        self.last_ok = None
        self.last_error = None
        self.open_requests = 0
        self._wake = asyncio.Event()

    # ── files shared with the host updater ──
    @property
    def req_file(self):
        return os.path.join(self.data, "update-request.json")

    @property
    def status_file(self):
        return os.path.join(self.data, "update-status.json")

    def update_status(self) -> dict | None:
        try:
            with open(self.status_file) as f:
                return json.load(f)
        except Exception:
            return None

    def request_update(self, ref="main", target=None, by="panel") -> dict:
        if os.path.exists(self.req_file):
            return {"queued": True, "already": True}
        st = self.update_status() or {}
        if st.get("state") == "running":
            return {"queued": False, "running": True}
        with open(self.req_file, "w") as f:
            json.dump({"ref": ref, "version": target, "by": by, "at": dt.datetime.utcnow().isoformat() + "Z", "from": version()}, f)
        return {"queued": True}

    @property
    def prefs(self) -> dict:
        return self.state.settings.setdefault("updates", {"auto": False, "allow_remote": False})

    # ── HTTP ──
    def _headers(self):
        h = self.state.settings.get("hub") or {}
        return {"Authorization": f"Bearer {h.get('key', '')}", "X-Instance": h.get("id", "")}

    async def call(self, method, path, payload=None):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.request(method, HUB_URL + path, json=payload, headers=self._headers()) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {"error": f"HTTP {r.status}"}
                if r.status == 401 and path != "/api/v1/register":
                    self.state.settings.pop("hub", None)   # key revoked/removed → re-register next round
                    self.state.save()
                if r.status >= 400:
                    raise core.EconError(data.get("error") or f"Hub error {r.status}")
                return data

    def info(self) -> dict:
        bot = self.state.bot
        g = bot.get_guild(self.state.guild_id) if bot and self.state.guild_id else None
        st = self.state.settings
        return {"name": st.get("bot_name") or (str(bot.user) if bot and bot.user else None), "version": version(),
                "guild_id": str(self.state.guild_id or ""), "guild_name": g.name if g else None,
                "guild_icon": g.icon.url if g and g.icon else None, "members": g.member_count if g else None,
                "panel_url": st.get("public_url") or st.get("seen_url"), "status": self.state.status,
                "features": {**core.DEFAULT_FEATURES, **(st.get("features") or {})},
                "auto_update": bool(self.prefs.get("auto")), "allow_remote": bool(self.prefs.get("allow_remote")),
                "owner_ids": [str(x) for x in st.get("owner_ids", [])]}

    async def ensure_registered(self):
        h = self.state.settings.get("hub") or {}
        if h.get("key"):
            return
        iid = h.get("id") or str(uuid.uuid4())
        self.state.settings["hub"] = {"id": iid}
        res = await self.call("POST", "/api/v1/register", {"instance_id": iid, **self.info()})
        self.state.settings["hub"] = {"id": iid, "key": res["api_key"]}
        self.state.save()
        log.info("registered with hub as %s", iid)

    async def heartbeat(self):
        await self.ensure_registered()
        payload = self.info()
        st = self.update_status()
        if st and st.get("state") in ("done", "failed") and not st.get("reported"):
            payload["update_result"] = {"ok": st.get("state") == "done", "from": st.get("from"), "to": st.get("to"),
                                        "log": (st.get("log") or "")[-3000:]}
        res = await self.call("POST", "/api/v1/heartbeat", payload)
        if payload.get("update_result"):
            st["reported"] = True
            with open(self.status_file, "w") as f:
                json.dump(st, f)
        self.latest, self.update_available = res.get("latest"), res.get("update_available")
        self.open_requests = res.get("open_requests", 0)
        self.last_ok, self.last_error = dt.datetime.utcnow(), None
        # remote push from the developer (only if the owner allowed it)
        push = res.get("push_update")
        if push and self.prefs.get("allow_remote") and self.prefs.get("last_push") != push.get("at"):
            self.prefs["last_push"] = push.get("at")
            self.state.save()
            self.request_update(push.get("ref") or "main", push.get("version"), "developer")
        # auto-update
        elif self.update_available and self.prefs.get("auto") and self.latest:
            failed = (st or {}).get("state") == "failed" and (st or {}).get("target") == self.latest["version"]
            if not failed:
                self.request_update(self.latest.get("ref") or "main", self.latest["version"], "auto-update")

    async def run(self):
        await asyncio.sleep(8)
        while True:
            if self.state.configured:
                try:
                    await self.heartbeat()
                except Exception as e:
                    self.last_error = str(e)
                    log.warning("hub heartbeat failed: %s", e)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), INTERVAL)
            except asyncio.TimeoutError:
                pass

    def poke(self):
        self._wake.set()
