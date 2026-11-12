"""Web panel: Discord OAuth login + JSON API + single-page UI. Runs inside the bot process."""
import os, json, time, hmac, hashlib, base64, secrets, urllib.parse, datetime as dt

import aiohttp
from html import escape as html_escape
from aiohttp import web
import discord

import core
from core import q, q1, ex, cfg, set_cfg, coerce_cfg, money, transfer, ensure_user, is_admin, org_level, EconError, \
    MINT, BURN, MEMBER, HEAD

API = "https://discord.com/api/v10"
SECRET = b""
BOT_PERMS = 268561488  # Manage Roles+Channels+Messages, View, Send, Embed, Attach, Read History, Add Reactions
STATIC = os.path.join(os.path.dirname(__file__), "static")
SESSION_DAYS = 7


# ───────── signed cookies ─────────
def sign(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    sig = hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def unsign(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


def jerr(msg, status=400):
    return web.json_response({"error": msg}, status=status)


class Panel:
    def __init__(self, state):
        global SECRET
        self.state = state
        SECRET = state.settings["web_secret"].encode()
        self.app = web.Application(middlewares=[self.mw])
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/login", self.login)
        r.add_get("/callback", self.callback)
        r.add_get("/logout", self.logout)
        r.add_post("/api/oauth", self.api_oauth)
        r.add_get("/api/setup/status", self.api_setup_status)
        r.add_post("/api/setup", self.api_setup)
        r.add_post("/api/setup/restart", self.api_setup_restart)
        r.add_post("/api/setup/token", self.api_setup_token)
        r.add_get("/api/setup/guilds", self.api_setup_guilds)
        r.add_get("/api/setup/redirect", self.api_setup_check_redirect)
        r.add_post("/api/setup/finish", self.api_setup_finish)
        r.add_get("/api/me", self.api_me)
        r.add_get("/api/my", self.api_my)
        r.add_post("/api/my/move", self.api_my_move)
        r.add_post("/api/my/pay_citation", self.api_my_pay_citation)
        r.add_get("/api/overview", self.api_overview)
        r.add_get("/api/users", self.api_users)
        r.add_get("/api/user/{uid}", self.api_user)
        r.add_get("/api/orgs", self.api_orgs)
        r.add_get("/api/org/{oid}", self.api_org)
        r.add_get("/api/citations", self.api_citations)
        r.add_get("/api/ledger", self.api_ledger)
        r.add_get("/api/settings", self.api_settings)
        r.add_post("/api/settings", self.api_settings_save)
        r.add_post("/api/admin/money", self.api_money)
        r.add_post("/api/admin/freeze", self.api_freeze)
        r.add_post("/api/admin/undo", self.api_undo)
        r.add_post("/api/admin/citation", self.api_citation_action)
        r.add_post("/api/admin/org", self.api_org_create)
        r.add_post("/api/org/{oid}/item", self.api_item_save)
        r.add_post("/api/org/{oid}/item/delete", self.api_item_delete)
        r.add_post("/api/org/{oid}/payroll", self.api_payroll_save)
        r.add_get("/api/cad", self.api_cad)
        r.add_post("/api/cad", self.api_cad_save)
        r.add_post("/api/cad/test", self.api_cad_test)
        r.add_static("/static", STATIC)
        import web_ext
        web_ext.register(self)
        self.runner = None
        self._names: dict[int, tuple[float, dict]] = {}

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, os.getenv("WEB_HOST", "0.0.0.0"), int(os.getenv("WEB_PORT", "8095")))
        await site.start()
        print(f"[web] listening on {os.getenv('WEB_HOST', '0.0.0.0')}:{os.getenv('WEB_PORT', '8095')}")

    # ───────── middleware / auth ─────────
    @web.middleware
    async def mw(self, request, handler):
        if request.method == "POST":
            if request.headers.get("X-Panel") != "1":   # CSRF guard: browsers can't add this cross-site
                return jerr("bad request", 403)
        try:
            resp = await handler(request)
        except EconError as e:
            return jerr(str(e))
        except web.HTTPException:
            raise
        except Exception as e:
            print(f"[web] {request.path}: {e!r}")
            return jerr("server error", 500)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @property
    def bot(self):
        return self.state.bot

    @property
    def gid(self):
        return self.state.guild_id

    @property
    def ready(self) -> bool:
        return bool(self.bot and self.bot.is_ready() and self.guild)

    @property
    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.gid) if (self.bot and self.gid) else None

    def redirect_uri(self, request) -> str:
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{proto}://{host}/callback"

    def invite_url(self) -> str | None:
        cid = self.state.settings.get("client_id")
        if not cid:
            return None
        q_ = {"client_id": cid, "scope": "bot applications.commands", "permissions": BOT_PERMS}
        if self.gid:
            q_["guild_id"] = self.gid
            q_["disable_guild_select"] = "true"
        return "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(q_)

    async def member(self, uid: int) -> discord.Member | None:
        g = self.guild
        if not g:
            return None
        m = g.get_member(uid)
        if m:
            return m
        try:
            return await g.fetch_member(uid)
        except discord.HTTPException:
            return None

    def owner_session(self, request) -> int | None:
        """Logged-in user listed as Bot Creator in settings (works even while the bot is offline)."""
        s = unsign(request.cookies.get("eb_session"))
        if s and int(s["uid"]) in core.BOT_OWNERS | {int(x) for x in self.state.settings.get("owner_ids", [])}:
            return int(s["uid"])
        return None

    async def session(self, request, admin=False, perm=None):
        s = unsign(request.cookies.get("eb_session"))
        if not s:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "login required"}), content_type="application/json")
        if not self.ready:
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": "bot offline", "offline": True}),
                                             content_type="application/json")
        m = await self.member(int(s["uid"]))
        if not m:
            raise web.HTTPForbidden(text=json.dumps({"error": "You're not in the server."}), content_type="application/json")
        c = await cfg(self.gid)
        adm = core.staff_perms(m, c)
        need = "*" if admin and not perm else perm
        if need and not (adm if need == "*" else need in adm):
            what = "Staff only." if need == "*" else f"You need the “{core.STAFF_PERMS[need][0]}” staff permission."
            raise web.HTTPForbidden(text=json.dumps({"error": what}), content_type="application/json")
        return m, adm, c

    async def body(self, request) -> dict:
        try:
            return await request.json()
        except Exception:
            raise EconError("Invalid JSON")

    # ───────── names ─────────
    async def who(self, uid: int) -> dict:
        uid = int(uid)
        hit = self._names.get(uid)
        if hit and hit[0] > time.time():
            return hit[1]
        if not self.bot:
            return {"id": str(uid), "name": f"User {uid}", "username": "", "avatar": ""}
        g = self.guild
        m = g.get_member(uid) if g else None
        if m:
            info = {"id": str(uid), "name": m.display_name, "username": m.name, "avatar": m.display_avatar.url}
        else:
            u = self.bot.get_user(uid)
            if u is None:
                try:
                    u = await self.bot.fetch_user(uid)
                except discord.HTTPException:
                    u = None
            info = {"id": str(uid), "name": u.display_name if u else f"User {uid}", "username": u.name if u else "",
                    "avatar": u.display_avatar.url if u else ""}
        self._names[uid] = (time.time() + 600, info)
        return info

    async def whos(self, ids) -> dict:
        return {str(i): await self.who(i) for i in {int(x) for x in ids if x}}

    # ───────── pages ─────────
    async def index(self, request):
        return web.FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})

    async def login(self, request):
        st = self.state.settings
        if not st.get("client_id"):
            raise web.HTTPFound("/")
        await self._ensure_redirect(request)
        state = secrets.token_urlsafe(24)
        # implicit grant: no client secret needed (code flow is used if a secret was configured)
        rtype = "code" if st.get("client_secret") else "token"
        url = f"https://discord.com/oauth2/authorize?" + urllib.parse.urlencode({
            "client_id": st["client_id"], "redirect_uri": self.redirect_uri(request), "response_type": rtype,
            "scope": "identify", "state": state, "prompt": "none"})
        resp = web.HTTPFound(url)
        nxt = request.query.get("next") or "/"
        if nxt.startswith("/") and not nxt.startswith("//"):
            resp.set_cookie("eb_next", nxt, max_age=600, httponly=True, samesite="Lax")
        resp.set_cookie("eb_state", state, max_age=600, httponly=True, samesite="Lax",
                        secure=self.redirect_uri(request).startswith("https"))
        raise resp

    async def _ensure_redirect(self, request):
        """After moving servers the panel's address changes — register the new login URL with Discord automatically."""
        st = self.state.settings
        uri = self.redirect_uri(request)
        if not st.get("token") or uri in (st.get("redirects_ok") or []):
            return
        try:
            code_, appinfo = await self._discord("GET", "/applications/@me", st["token"])
            uris = list(appinfo.get("redirect_uris") or []) if code_ == 200 else []
            if code_ == 200 and uri not in uris:
                code_, data = await self._discord("PATCH", "/applications/@me", st["token"], json={"redirect_uris": uris + [uri]})
                uris = data.get("redirect_uris") or [] if code_ == 200 else uris
            if uri in uris:
                st["redirects_ok"] = sorted(set(st.get("redirects_ok") or []) | {uri})
                self.state.save()
        except Exception as e:
            print(f"[web] redirect check failed: {e!r}")

    async def callback(self, request):
        if request.query.get("error"):
            return self._login_fail(request.query.get("error_description") or request.query["error"])
        code, state = request.query.get("code"), request.query.get("state")
        if not code:
            # implicit grant — token arrives in the URL fragment; hand it to /api/oauth
            return web.Response(content_type="text/html", text="""<!doctype html><meta charset=utf-8><title>Signing in…</title>
<body style="background:#060a12;color:#cbd5e1;font:15px system-ui;display:grid;place-items:center;height:100vh">Signing you in…
<script>const h=new URLSearchParams(location.hash.slice(1));history.replaceState(null,'',location.pathname);
fetch('/api/oauth',{method:'POST',headers:{'Content-Type':'application/json','X-Panel':'1'},body:JSON.stringify({access_token:h.get('access_token'),state:h.get('state'),error:h.get('error_description')||h.get('error')})})
.then(r=>r.json()).then(j=>{if(j.redirect)location.replace(j.redirect);else document.body.innerHTML='<div style="max-width:420px;text-align:center"><h2>Login failed</h2><p>'+(j.error||'Unknown error').replace(/</g,'&lt;')+'</p><a style="color:#38b6ff" href="/">Back</a></div>'})</script>""")
        if not state or state != request.cookies.get("eb_state"):
            return self._login_fail("Login expired — please try again.")
        st = self.state.settings
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API}/oauth2/token", data={
                "client_id": st.get("client_id"), "client_secret": st.get("client_secret"),
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri(request)}) as r:
                tok = await r.json()
        if "access_token" not in tok:
            return self._login_fail(tok.get("error_description") or str(tok))
        target, err = await self._finish_login(request, tok["access_token"])
        if err:
            return self._login_fail(err)
        resp = web.HTTPFound(target["redirect"])
        self._set_session(request, resp, target["uid"])
        raise resp

    def _login_fail(self, msg):
        return web.Response(content_type="text/html", status=400, text=f"""<!doctype html><meta charset=utf-8><title>Login failed</title>
<body style="background:#060a12;color:#cbd5e1;font:15px system-ui;display:grid;place-items:center;height:100vh"><div style="max-width:440px;text-align:center">
<h2>Login failed</h2><p>{html_escape(msg)}</p><a style="color:#38b6ff" href="/">Back</a></div>""")

    def _set_session(self, request, resp, uid):
        resp.del_cookie("eb_next")
        resp.del_cookie("eb_state")
        resp.set_cookie("eb_session", sign({"uid": str(uid), "exp": time.time() + SESSION_DAYS * 86400}),
                        max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax",
                        secure=self.redirect_uri(request).startswith("https"))

    async def _finish_login(self, request, access_token):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as r:
                if r.status != 200:
                    return None, "Discord didn't accept the login."
                user = await r.json()
        st = self.state.settings
        uid = int(user["id"])
        base = self.redirect_uri(request).rsplit("/", 1)[0]
        if st.get("seen_url") != base:
            st["seen_url"] = base
            self.state.save()
        is_owner = uid in {int(x) for x in st.get("owner_ids", [])}
        if not is_owner and self.ready and not await self.member(uid):
            return None, "You need to be a member of the Discord server to use this panel."
        if not is_owner and not self.ready:
            return None, "The bot is offline right now — try again shortly."
        nxt = request.cookies.get("eb_next") or "/"
        return {"uid": uid, "redirect": nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"}, None

    async def api_oauth(self, request):
        b = await self.body(request)
        if b.get("error"):
            return jerr(f"Discord said: {b['error']}. If it mentions redirect_uri, add {self.redirect_uri(request)} under OAuth2 → Redirects.")
        if not b.get("access_token") or not b.get("state") or b["state"] != request.cookies.get("eb_state"):
            return jerr("Login expired — please try again.")
        target, err = await self._finish_login(request, b["access_token"])
        if err:
            return jerr(err)
        resp = web.json_response({"redirect": target["redirect"]})
        self._set_session(request, resp, target["uid"])
        return resp

    async def logout(self, request):
        resp = web.HTTPFound("/")
        resp.del_cookie("eb_session")
        raise resp

    # ───────── member API ─────────
    async def api_me(self, request):
        m, adm, c = await self.session(request)
        g = self.guild
        feats = {**core.DEFAULT_FEATURES, **(self.state.settings.get("features") or {})}
        counts = {}
        if adm:
            counts = {}
        if "tickets" in adm:
            counts["tickets"] = (await q1("SELECT COUNT(*) n FROM tickets WHERE guild_id=%s AND status='open'", self.gid))["n"]
        if "applications" in adm:
            counts["applications"] = (await q1("SELECT COUNT(*) n FROM applications WHERE guild_id=%s AND status='pending'", self.gid))["n"]
        if "citations" in adm:
            counts["citations"] = (await q1("SELECT COUNT(*) n FROM citations WHERE guild_id=%s AND status='unpaid'", self.gid))["n"]
        return web.json_response({"user": await self.who(m.id), "admin": bool(adm), "perms": sorted(adm),
                                  "ranks": core.staff_ranks_of(m, c), "currency": c["currency"], "features": feats,
                                  "onboarded": bool(c.get("onboarded")), "counts": counts,
                                  "brand": {"name": c.get("brand_name"), "full": c.get("brand_full"), "color": c.get("brand_color")},
                                  "guild": {"name": g.name, "icon": g.icon.url if g.icon else ""}})

    async def api_my(self, request):
        m, adm, c = await self.session(request)
        gid = self.gid
        await ensure_user(gid, m.id)
        u = await q1("SELECT * FROM users WHERE guild_id=%s AND user_id=%s", gid, m.id)
        items = await q("SELECT name, description, qty, source FROM user_items WHERE guild_id=%s AND user_id=%s AND qty>0 "
                        "ORDER BY name", gid, m.id)
        cits = await q("SELECT c.*, o.name dept FROM citations c LEFT JOIN orgs o ON o.id=c.dept_id WHERE c.guild_id=%s "
                       "AND c.user_id=%s ORDER BY c.id DESC LIMIT 50", gid, m.id)
        hist = await q("SELECT * FROM ledger WHERE guild_id=%s AND ((src_type IN ('wallet','bank') AND src_id=%s) OR "
                       "(dst_type IN ('wallet','bank') AND dst_id=%s)) ORDER BY id DESC LIMIT 50", gid, m.id, m.id)
        orgs = []
        for o in await q("SELECT * FROM orgs WHERE guild_id=%s ORDER BY kind,name", gid):
            lvl = await org_level(m, o, ignore_admin=True)
            if lvl >= MEMBER:
                orgs.append({"id": o["id"], "kind": o["kind"], "name": o["name"], "balance": o["balance"],
                             "level": "head" if lvl >= HEAD else "member"})
        return web.json_response({"wallet": u["wallet"], "bank": u["bank"], "frozen": bool(u["frozen"]),
                                  "items": items, "citations": [self._cit(r) for r in cits],
                                  "history": await self._ledger_rows(hist), "orgs": orgs}, dumps=_dumps)

    async def api_my_move(self, request):
        m, adm, c = await self.session(request)
        b = await self.body(request)
        direction = b.get("direction")
        u = await q1("SELECT * FROM users WHERE guild_id=%s AND user_id=%s", self.gid, m.id)
        if direction == "deposit":
            amt = core.parse_amount(str(b.get("amount")), u["wallet"])
            await transfer(self.gid, [(("wallet", m.id), ("bank", m.id), amt)], m.id, "deposit", "Deposit (web)")
        elif direction == "withdraw":
            amt = core.parse_amount(str(b.get("amount")), u["bank"])
            await transfer(self.gid, [(("bank", m.id), ("wallet", m.id), amt)], m.id, "withdraw", "Withdrawal (web)")
        else:
            raise EconError("Unknown action")
        return web.json_response({"ok": True})

    async def api_my_pay_citation(self, request):
        m, adm, c = await self.session(request)
        b = await self.body(request)
        cid = int(b.get("id", 0))
        r = await q1("SELECT * FROM citations WHERE guild_id=%s AND id=%s", self.gid, cid)
        if not r or r["status"] != "unpaid":
            raise EconError("That citation isn't payable.")
        await ensure_user(self.gid, m.id)
        dst = ("org", r["dept_id"]) if r["dept_id"] else (("org", c["treasury_dept"]) if c["treasury_dept"] else BURN)

        async def extra(cur):
            await cur.execute("UPDATE citations SET status='paid', paid_at=UTC_TIMESTAMP(), paid_by=%s "
                              "WHERE id=%s AND status='unpaid'", (m.id, cid))
            if cur.rowcount != 1:
                raise EconError("Already paid.")
        total = r["amount"] + r["late_fee"]
        await transfer(self.gid, [(("bank", m.id), dst, total)], m.id, "citation", f"Citation #{cid} (web)", extra=extra)
        await core.citation_paid(cid)
        await core.log(self.bot, self.gid, core.emb("✅ Citation paid (web)", f"#{cid} · **{money(c, total)}** by <@{m.id}>",
                                                     core.GREEN))
        return web.json_response({"ok": True})

    # ───────── admin API ─────────
    async def api_overview(self, request):
        m, adm, c = await self.session(request, perm="*")
        gid = self.gid
        u = await q1("SELECT COUNT(*) n, COALESCE(SUM(wallet),0) w, COALESCE(SUM(bank),0) b FROM users WHERE guild_id=%s", gid)
        o = await q1("SELECT COALESCE(SUM(CASE WHEN kind='business' THEN balance END),0) biz, "
                     "COALESCE(SUM(CASE WHEN kind='dept' THEN balance END),0) dep, "
                     "SUM(kind='business') nb, SUM(kind='dept') nd FROM orgs WHERE guild_id=%s", gid)
        cit = await q1("SELECT COUNT(*) n, COALESCE(SUM(amount+late_fee),0) s FROM citations WHERE guild_id=%s AND status='unpaid'", gid)
        daily = await q("SELECT DATE(ts) d, COUNT(*) n, SUM(amount) s, "
                        "SUM(CASE WHEN src_type='mint' THEN amount ELSE 0 END) minted, "
                        "SUM(CASE WHEN dst_type='burn' THEN amount ELSE 0 END) burned "
                        "FROM ledger WHERE guild_id=%s AND ts > UTC_TIMESTAMP() - INTERVAL 14 DAY GROUP BY DATE(ts) ORDER BY d", gid)
        top = await q("SELECT user_id, wallet+bank total FROM users WHERE guild_id=%s ORDER BY total DESC LIMIT 10", gid)
        kinds = await q("SELECT kind, COUNT(*) n, SUM(amount) s FROM ledger WHERE guild_id=%s AND ts > UTC_TIMESTAMP() - "
                        "INTERVAL 7 DAY GROUP BY kind ORDER BY s DESC", gid)
        names = await self.whos([t["user_id"] for t in top])
        return web.json_response({
            "accounts": u["n"], "wallets": int(u["w"]), "banks": int(u["b"]), "business": int(o["biz"]),
            "depts": int(o["dep"]), "n_business": int(o["nb"] or 0), "n_depts": int(o["nd"] or 0),
            "unpaid_citations": cit["n"], "unpaid_total": int(cit["s"]),
            "daily": [{"d": str(r["d"]), "n": r["n"], "s": int(r["s"] or 0), "minted": int(r["minted"] or 0),
                       "burned": int(r["burned"] or 0)} for r in daily],
            "top": [{"user": names[str(t["user_id"])], "total": int(t["total"])} for t in top],
            "kinds": [{"kind": k["kind"], "n": k["n"], "s": int(k["s"] or 0)} for k in kinds],
        })

    async def api_users(self, request):
        await self.session(request, perm="economy")
        search = (request.query.get("q") or "").strip().lower()
        rows = await q("SELECT u.*, (SELECT COUNT(*) FROM citations c WHERE c.guild_id=u.guild_id AND c.user_id=u.user_id "
                       "AND c.status='unpaid') cits FROM users u WHERE guild_id=%s ORDER BY wallet+bank DESC LIMIT 500", self.gid)
        out = []
        for r in rows:
            w = await self.who(r["user_id"])
            if search and search not in w["name"].lower() and search not in w["username"].lower() and search not in w["id"]:
                continue
            out.append({"user": w, "wallet": r["wallet"], "bank": r["bank"], "frozen": bool(r["frozen"]),
                        "citations": r["cits"], "created": str(r["created_at"])})
        return web.json_response(out[:200])

    async def api_user(self, request):
        await self.session(request, perm="economy")
        uid = int(request.match_info["uid"])
        u = await q1("SELECT * FROM users WHERE guild_id=%s AND user_id=%s", self.gid, uid)
        if not u:
            return jerr("No account", 404)
        items = await q("SELECT name, qty, source FROM user_items WHERE guild_id=%s AND user_id=%s AND qty>0", self.gid, uid)
        cits = await q("SELECT c.*, o.name dept FROM citations c LEFT JOIN orgs o ON o.id=c.dept_id WHERE c.guild_id=%s "
                       "AND c.user_id=%s ORDER BY id DESC LIMIT 50", self.gid, uid)
        hist = await q("SELECT * FROM ledger WHERE guild_id=%s AND ((src_type IN ('wallet','bank') AND src_id=%s) OR "
                       "(dst_type IN ('wallet','bank') AND dst_id=%s)) ORDER BY id DESC LIMIT 100", self.gid, uid, uid)
        biz = await q("SELECT id,name,balance FROM orgs WHERE guild_id=%s AND owner_id=%s", self.gid, uid)
        return web.json_response({"user": await self.who(uid), "wallet": u["wallet"], "bank": u["bank"],
                                  "frozen": bool(u["frozen"]), "items": items, "businesses": biz,
                                  "citations": [self._cit(r) for r in cits], "history": await self._ledger_rows(hist)})

    async def api_orgs(self, request):
        await self.session(request, perm="orgs")
        rows = await q("SELECT o.*, (SELECT COUNT(*) FROM org_items i WHERE i.org_id=o.id) items, "
                       "(SELECT COUNT(*) FROM payroll p WHERE p.org_id=o.id) staff, "
                       "(SELECT COALESCE(SUM(amount),0) FROM payroll p WHERE p.org_id=o.id) payroll "
                       "FROM orgs o WHERE guild_id=%s ORDER BY kind, name", self.gid)
        names = await self.whos([r["owner_id"] for r in rows])
        return web.json_response([{"id": r["id"], "kind": r["kind"], "name": r["name"], "balance": r["balance"],
                                   "owner": names.get(str(r["owner_id"])) if r["owner_id"] else None,
                                   "items": r["items"], "staff": r["staff"], "payroll": int(r["payroll"]),
                                   "description": r["description"]} for r in rows])

    async def api_org(self, request):
        await self.session(request, perm="orgs")
        oid = int(request.match_info["oid"])
        o = await q1("SELECT * FROM orgs WHERE guild_id=%s AND id=%s", self.gid, oid)
        if not o:
            return jerr("Not found", 404)
        items = await q("SELECT * FROM org_items WHERE org_id=%s ORDER BY name", oid)
        roles = await q("SELECT * FROM org_roles WHERE org_id=%s", oid)
        pay = await q("SELECT * FROM payroll WHERE org_id=%s ORDER BY amount DESC", oid)
        hist = await q("SELECT * FROM ledger WHERE guild_id=%s AND ((src_type='org' AND src_id=%s) OR (dst_type='org' AND dst_id=%s)) "
                       "ORDER BY id DESC LIMIT 100", self.gid, oid, oid)
        names = await self.whos([p["user_id"] for p in pay] + [o["owner_id"]])
        g = self.guild
        role_info = [{"id": str(r["role_id"]), "level": r["level"],
                      "name": (g.get_role(r["role_id"]).name if g and g.get_role(r["role_id"]) else str(r["role_id"]))}
                     for r in roles]
        return web.json_response({
            "org": {"id": o["id"], "kind": o["kind"], "name": o["name"], "balance": o["balance"],
                    "description": o["description"], "owner": names.get(str(o["owner_id"])) if o["owner_id"] else None},
            "items": [{**i, "role_id": str(i["role_id"]) if i["role_id"] else None,
                       "role_name": (g.get_role(i["role_id"]).name if g and i["role_id"] and g.get_role(i["role_id"]) else None)}
                      for i in items],
            "roles": role_info,
            "payroll": [{"user": names[str(p["user_id"])], "amount": p["amount"], "last_paid": _iso(p["last_paid"])} for p in pay],
            "history": await self._ledger_rows(hist),
        }, dumps=_dumps)

    async def api_citations(self, request):
        await self.session(request, perm="citations")
        status = request.query.get("status", "unpaid")
        cond = "" if status == "all" else " AND c.status=%s"
        args = [self.gid] + ([] if status == "all" else [status])
        rows = await q(f"SELECT c.*, o.name dept FROM citations c LEFT JOIN orgs o ON o.id=c.dept_id WHERE c.guild_id=%s{cond} "
                       "ORDER BY c.id DESC LIMIT 300", *args)
        names = await self.whos([r["user_id"] for r in rows] + [r["issuer_id"] for r in rows])
        return web.json_response([{**self._cit(r), "user": names[str(r["user_id"])], "issuer": names[str(r["issuer_id"])]}
                                  for r in rows])

    async def api_ledger(self, request):
        await self.session(request, perm="economy")
        before = int(request.query.get("before") or 0)
        kind = request.query.get("kind") or ""
        sql = "SELECT * FROM ledger WHERE guild_id=%s"
        args = [self.gid]
        if before:
            sql += " AND id < %s"; args.append(before)
        if kind:
            sql += " AND kind=%s"; args.append(kind)
        rows = await q(sql + " ORDER BY id DESC LIMIT 100", *args)
        return web.json_response(await self._ledger_rows(rows))

    async def api_settings(self, request):
        await self.session(request, perm="setup")
        c = dict(await cfg(self.gid))
        g = self.guild
        depts = await q("SELECT id,name FROM orgs WHERE guild_id=%s AND kind='dept' ORDER BY name", self.gid)
        return web.json_response({
            "settings": {k: (str(v) if isinstance(v, int) and v > 10**15 else v) for k, v in c.items() if k in core.DEFAULTS},
            "channels": [{"id": str(ch.id), "name": ch.name} for ch in (g.text_channels if g else [])],
            "roles": [{"id": str(r.id), "name": r.name} for r in (g.roles if g else []) if not r.is_default()][::-1],
            "depts": depts,
            "bot": self._bot_info(request),
        })

    async def api_settings_save(self, request):
        m, adm, c = await self.session(request, perm="setup")
        b = await self.body(request)
        changes = {}
        for k, v in b.items():
            if k not in core.DEFAULTS:
                continue
            changes[k] = coerce_cfg(k, "" if v is None else str(v))
        await set_cfg(self.gid, **changes)
        await core.log(self.bot, self.gid, core.emb("⚙️ Settings changed (web)",
                       ", ".join(f"`{k}`" for k in changes) + f" by <@{m.id}>", core.GOLD))
        return web.json_response({"ok": True})

    async def api_money(self, request):
        m, adm, c = await self.session(request, perm="economy")
        b = await self.body(request)
        op, amount, reason = b.get("op"), str(b.get("amount", "")), (b.get("reason") or "")[:200]
        if b.get("user"):
            uid = int(b["user"])
            await ensure_user(self.gid, uid)
            where = b.get("into") if b.get("into") in ("wallet", "bank") else "bank"
            ref, label = (where, uid), f"<@{uid}>"
            cur = (await q1("SELECT * FROM users WHERE guild_id=%s AND user_id=%s", self.gid, uid))[where]
        elif b.get("org"):
            o = await q1("SELECT * FROM orgs WHERE guild_id=%s AND id=%s", self.gid, int(b["org"]))
            if not o:
                raise EconError("Org not found")
            ref, label, cur = ("org", o["id"]), f"**{o['name']}**", o["balance"]
        else:
            raise EconError("Pick a user or org")
        if op == "print":
            amt = core.parse_amount(amount)
            await transfer(self.gid, [(MINT, ref, amt)], m.id, "admin_print", reason or "Admin print (web)")
            title = "🖨️ Money printed (web)"
        elif op == "remove":
            amt = core.parse_amount(amount, cur)
            await transfer(self.gid, [(ref, BURN, amt)], m.id, "admin_remove", reason or "Admin remove (web)")
            title = "🔥 Money removed (web)"
        else:
            raise EconError("Unknown op")
        await core.log(self.bot, self.gid, core.emb(title, f"<@{m.id}> · {label} · **{money(c, amt)}**"
                                                    + (f"\n> {reason}" if reason else ""), core.GOLD))
        return web.json_response({"ok": True})

    async def api_freeze(self, request):
        m, adm, c = await self.session(request, perm="economy")
        b = await self.body(request)
        uid = int(b["user"])
        await ex("UPDATE users SET frozen=%s WHERE guild_id=%s AND user_id=%s", bool(b.get("frozen")), self.gid, uid)
        await core.log(self.bot, self.gid, core.emb("🧊 Freeze (web)", f"<@{uid}> frozen={bool(b.get('frozen'))} by <@{m.id}>", core.GOLD))
        return web.json_response({"ok": True})

    async def api_undo(self, request):
        m, adm, c = await self.session(request, perm="economy")
        b = await self.body(request)
        tid = int(b["id"])
        r = await q1("SELECT * FROM ledger WHERE guild_id=%s AND id=%s", self.gid, tid)
        if not r or r["reversed_by"] or r["kind"] == "undo":
            raise EconError("That transaction can't be reversed.")
        for t, i in ((r["src_type"], r["src_id"]), (r["dst_type"], r["dst_id"])):
            if t in ("wallet", "bank"):
                await ensure_user(self.gid, i)

        async def extra(cur):
            await cur.execute("UPDATE ledger SET reversed_by=LAST_INSERT_ID() WHERE id=%s", (tid,))
        await transfer(self.gid, [((r["dst_type"], r["dst_id"]), (r["src_type"], r["src_id"]), r["amount"])], m.id,
                       "undo", f"Reverse #{tid} (web)", extra=extra, allow_negative=bool(b.get("force")))
        await core.log(self.bot, self.gid, core.emb("↩️ Reversed (web)", f"#{tid} by <@{m.id}>", core.GOLD))
        return web.json_response({"ok": True})

    async def api_citation_action(self, request):
        m, adm, c = await self.session(request, perm="citations")
        b = await self.body(request)
        cid, action = int(b["id"]), b.get("action")
        if action == "void":
            n, _ = await ex("UPDATE citations SET status='void', note=%s WHERE guild_id=%s AND id=%s AND status='unpaid'",
                            (b.get("note") or f"Voided via web by {m}")[:300], self.gid, cid)
        elif action == "waive_fee":
            n, _ = await ex("UPDATE citations SET late_fee=0, due_at=UTC_TIMESTAMP() + INTERVAL 7 DAY "
                            "WHERE guild_id=%s AND id=%s AND status='unpaid'", self.gid, cid)
        else:
            raise EconError("Unknown action")
        if not n:
            raise EconError("Citation isn't unpaid.")
        await core.log(self.bot, self.gid, core.emb("🚨 Citation updated (web)", f"#{cid} {action} by <@{m.id}>", core.GOLD))
        return web.json_response({"ok": True})

    async def api_org_create(self, request):
        m, adm, c = await self.session(request, perm="orgs")
        b = await self.body(request)
        kind = b.get("kind")
        name = (b.get("name") or "").strip()[:64]
        if kind not in ("business", "dept") or len(name) < 2:
            raise EconError("Need kind and a name (2+ chars)")
        owner = int(b["owner"]) if kind == "business" and str(b.get("owner") or "").isdigit() else None
        if kind == "business" and not owner:
            raise EconError("Businesses need an owner (Discord user ID)")
        if await q1("SELECT 1 FROM orgs WHERE guild_id=%s AND kind=%s AND name=%s", self.gid, kind, name):
            raise EconError("Name taken")
        if owner:
            await ensure_user(self.gid, owner)
        _, oid = await ex("INSERT INTO orgs (guild_id,kind,name,owner_id,description) VALUES (%s,%s,%s,%s,%s)",
                          self.gid, kind, name, owner, (b.get("description") or "")[:500] or None)
        for lvl in ("head", "member"):
            rid = str(b.get(f"{lvl}_role") or "")
            if rid.isdigit():
                await ex("INSERT IGNORE INTO org_roles VALUES (%s,%s,%s)", oid, int(rid), lvl)
        await core.log(self.bot, self.gid, core.emb("🏗️ Created (web)", f"{kind} **{name}** by <@{m.id}>", core.GREEN))
        return web.json_response({"ok": True, "id": oid})

    async def api_item_save(self, request):
        m, adm, c = await self.session(request, perm="orgs")
        oid = int(request.match_info["oid"])
        if not await q1("SELECT 1 FROM orgs WHERE guild_id=%s AND id=%s", self.gid, oid):
            raise EconError("Org not found")
        b = await self.body(request)
        name = (b.get("name") or "").strip()[:64]
        if not name:
            raise EconError("Item needs a name")
        price = 0 if str(b.get("price", "0")).strip() in ("", "0") else core.parse_amount(str(b["price"]))
        stock = None if str(b.get("stock", "")).strip() in ("", "-1") else max(0, int(b["stock"]))
        vals = (name, (b.get("description") or "")[:300] or None, price, stock, bool(b.get("for_sale")),
                bool(b.get("consumable", True)), (b.get("use_message") or "")[:300] or None,
                int(b["role_id"]) if str(b.get("role_id") or "").isdigit() else None,
                int(b["role_days"]) if str(b.get("role_days") or "").isdigit() and int(b["role_days"]) > 0 else None)
        if b.get("id"):
            await ex("UPDATE org_items SET name=%s,description=%s,price=%s,stock=%s,for_sale=%s,consumable=%s,use_message=%s,"
                     "role_id=%s,role_days=%s WHERE id=%s AND org_id=%s", *vals, int(b["id"]), oid)
        else:
            try:
                await ex("INSERT INTO org_items (name,description,price,stock,for_sale,consumable,use_message,role_id,role_days,org_id) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", *vals, oid)
            except Exception:
                raise EconError("An item with that name already exists")
        return web.json_response({"ok": True})

    async def api_item_delete(self, request):
        await self.session(request, perm="orgs")
        b = await self.body(request)
        await ex("DELETE FROM org_items WHERE id=%s AND org_id=%s", int(b["id"]), int(request.match_info["oid"]))
        return web.json_response({"ok": True})

    async def api_payroll_save(self, request):
        m, adm, c = await self.session(request, perm="orgs")
        oid = int(request.match_info["oid"])
        if not await q1("SELECT 1 FROM orgs WHERE guild_id=%s AND id=%s", self.gid, oid):
            raise EconError("Org not found")
        b = await self.body(request)
        uid = int(b["user"])
        if b.get("remove"):
            await ex("DELETE FROM payroll WHERE org_id=%s AND user_id=%s", oid, uid)
        else:
            await ensure_user(self.gid, uid)
            await ex("INSERT INTO payroll (org_id,user_id,amount,added_by,last_paid) VALUES (%s,%s,%s,%s,UTC_TIMESTAMP()) "
                     "ON DUPLICATE KEY UPDATE amount=VALUES(amount)", oid, uid, core.parse_amount(str(b["amount"])), m.id)
        return web.json_response({"ok": True})

    # ───────── SnailyCAD ─────────
    def _cad_merged(self, b: dict) -> dict:
        cur = dict(self.state.settings.get("cad") or {})
        for k in ("host", "port", "user", "database", "url", "dept_id"):
            if k in b:
                cur[k] = str(b[k] or "").strip()
        if b.get("password"):
            cur["password"] = b["password"]
        for k in ("enabled", "sync_tickets", "mark_paid"):
            if k in b:
                cur[k] = bool(b[k])
        return cur

    async def api_cad(self, request):
        await self.session(request, perm="setup")
        s = dict(self.state.settings.get("cad") or {})
        has_pw = bool(s.pop("password", None))
        cog = self.bot.get_cog("CAD") if self.bot else None
        return web.json_response({**s, "has_password": has_pw, "last_error": cog.last_error if cog else None,
                                  "last_sync": _iso(cog.last_sync) if cog and cog.last_sync else None})

    async def api_cad_test(self, request):
        await self.session(request, perm="setup")
        from cogs.cad import test_connection
        try:
            return web.json_response({"ok": True, **await test_connection(self._cad_merged(await self.body(request)))})
        except Exception as e:
            raise EconError(f"Couldn't connect: {e}")

    async def api_cad_save(self, request):
        m, adm, c = await self.session(request, perm="setup")
        b = await self.body(request)
        new = self._cad_merged(b)
        old = self.state.settings.get("cad") or {}
        if new.get("enabled") and not old.get("enabled"):
            # only import tickets written from now on, unless asked to back-fill
            new["since"] = "2000-01-01 00:00:00" if b.get("import_old") else dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self.state.settings["cad"] = new
        self.state.save()
        cog = self.bot.get_cog("CAD") if self.bot else None
        if cog:
            await cog.reconnect()
        await core.log(self.bot, self.gid, core.emb("🔗 SnailyCAD settings changed (web)",
                                                     f"enabled={new.get('enabled')} by <@{m.id}>", core.GOLD))
        return web.json_response({"ok": True})

    # ───────── setup ─────────
    def _bot_info(self, request):
        st = self.state.settings
        return {"configured": self.state.configured, "status": self.state.status, "error": self.state.error,
                "warning": self.state.warning, "bot_name": st.get("bot_name"), "client_id": st.get("client_id"),
                "guild_id": str(st.get("guild_id") or ""), "owner_ids": ",".join(map(str, st.get("owner_ids", []))),
                "has_secret": bool(st.get("client_secret")), "bot_avatar": st.get("bot_avatar"), "public_url": st.get("public_url") or "",
                "redirect_uri": self.redirect_uri(request), "invite": self.invite_url(),
                "guild_name": self.guild.name if self.guild else None}

    async def setup_allowed(self, request, code: str | None) -> bool:
        if not self.state.configured:      # first-run: open until a server is connected
            return True
        if code and hmac.compare_digest(code.strip().upper(), self.state.setup_code):
            return True
        tok = unsign(request.cookies.get("eb_setup"))
        if tok and tok.get("setup"):
            return True
        if self.owner_session(request):
            return True
        if self.ready:
            s = unsign(request.cookies.get("eb_session"))
            if s:
                m = await self.member(int(s["uid"]))
                return bool(m and is_admin(m, await cfg(self.gid), "setup"))
        return False

    async def api_setup_status(self, request):
        info = self._bot_info(request)
        info["can_edit"] = await self.setup_allowed(request, None)
        info["has_token"] = bool(self.state.settings.get("token"))
        info["bot_avatar"] = self.state.settings.get("bot_avatar")
        if not info["can_edit"]:
            info = {k: info[k] for k in ("configured", "status", "redirect_uri")} | {"can_edit": False}
        return web.json_response(info)

    async def _discord(self, method, path, token, **kw):
        async with aiohttp.ClientSession() as s:
            async with s.request(method, f"{API}{path}", headers={"Authorization": f"Bot {token}"}, **kw) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {}
                return r.status, data

    async def api_setup_token(self, request):
        """Step 1: validate the bot token, auto-fix what Discord lets us fix (members intent, login redirect)."""
        b = await self.body(request)
        if not await self.setup_allowed(request, b.get("code")):
            raise EconError("Wrong setup code — it was printed when you installed (also in data/setup-code.txt).")
        token = (b.get("token") or "").strip().strip('"')
        if not token and self.state.settings.get("token"):
            token = self.state.settings["token"]
        if not token:
            raise EconError("Paste your bot token.")
        st_, appinfo = await self._discord("GET", "/applications/@me", token)
        if st_ != 200:
            raise EconError("Discord rejected that token. In the Developer Portal → Bot, press Reset Token and copy the new one.")
        redirect = self.redirect_uri(request)
        flags = int(appinfo.get("flags") or 0)
        members_ok = bool(flags & ((1 << 14) | (1 << 15)))
        fixed = []
        if not members_ok:   # bots under 100 servers may switch the *limited* members intent on themselves
            code_, _ = await self._discord("PATCH", "/applications/@me", token, json={"flags": flags | (1 << 15)})
            if code_ == 200:
                members_ok = True
                fixed.append("Turned on the Server Members intent")
        uris = list(appinfo.get("redirect_uris") or [])
        redirect_ok = redirect in uris
        if not redirect_ok:
            code_, data = await self._discord("PATCH", "/applications/@me", token, json={"redirect_uris": uris + [redirect]})
            if code_ == 200 and redirect in (data.get("redirect_uris") or []):
                redirect_ok = True
                fixed.append("Added the panel's login address to Discord")
        st = self.state.settings
        st.update(token=token, client_id=str(appinfo["id"]), bot_name=appinfo.get("name"),
                  bot_avatar=(f"https://cdn.discordapp.com/app-icons/{appinfo['id']}/{appinfo['icon']}.png" if appinfo.get("icon") else None))
        if not st.get("owner_ids") and appinfo.get("owner", {}).get("id"):
            st["owner_ids"] = [int(appinfo["owner"]["id"])]
        if appinfo.get("team"):
            st["owner_ids"] = sorted({int(m_["user"]["id"]) for m_ in appinfo["team"].get("members", [])} | set(st.get("owner_ids", [])))
        self.state.save()
        core.BOT_OWNERS.update(int(x) for x in st.get("owner_ids", []))
        resp = web.json_response({"ok": True, "bot_name": st["bot_name"], "bot_avatar": st.get("bot_avatar"), "client_id": st["client_id"],
                                  "members_ok": members_ok, "redirect_ok": redirect_ok, "redirect_uri": redirect, "fixed": fixed,
                                  "portal": f"https://discord.com/developers/applications/{st['client_id']}",
                                  "invite": self.invite_url_any(), "guilds": await self._bot_guilds(token)})
        resp.set_cookie("eb_setup", sign({"setup": True, "exp": time.time() + 3600}), max_age=3600, httponly=True, samesite="Lax")
        return resp

    def invite_url_any(self):
        cid = self.state.settings.get("client_id")
        return "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(
            {"client_id": cid, "scope": "bot applications.commands", "permissions": BOT_PERMS}) if cid else None

    async def _bot_guilds(self, token):
        st_, data = await self._discord("GET", "/users/@me/guilds", token)
        if st_ != 200:
            return []
        return [{"id": g["id"], "name": g["name"],
                 "icon": f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("icon") else None} for g in data]

    async def api_setup_guilds(self, request):
        if not await self.setup_allowed(request, None):
            raise EconError("Not allowed.")
        tok = self.state.settings.get("token")
        return web.json_response({"guilds": await self._bot_guilds(tok) if tok else []})

    async def api_setup_check_redirect(self, request):
        if not await self.setup_allowed(request, None):
            raise EconError("Not allowed.")
        st_, appinfo = await self._discord("GET", "/applications/@me", self.state.settings.get("token", ""))
        return web.json_response({"redirect_ok": self.redirect_uri(request) in (appinfo.get("redirect_uris") or []) if st_ == 200 else None})

    async def api_setup_finish(self, request):
        b = await self.body(request)
        if not await self.setup_allowed(request, b.get("code")):
            raise EconError("Not allowed.")
        gid = str(b.get("guild_id") or "")
        if not gid.isdigit():
            raise EconError("Pick your server.")
        st = self.state.settings
        st["guild_id"] = int(gid)
        if "public_url" in b:
            st["public_url"] = (b.get("public_url") or "").strip().rstrip("/")
        self.state.save()
        await self.state.start_bot()
        return web.json_response({"ok": True})

    async def api_setup(self, request):
        """Advanced: change token/server/owners in one go (Settings → Bot connection)."""
        b = await self.body(request)
        if not await self.setup_allowed(request, b.get("code")):
            raise EconError("Not allowed.")
        st = self.state.settings
        if (b.get("token") or "").strip():
            code_, appinfo = await self._discord("GET", "/applications/@me", b["token"].strip())
            if code_ != 200:
                raise EconError("Discord rejected that bot token.")
            st.update(token=b["token"].strip(), client_id=str(appinfo["id"]), bot_name=appinfo.get("name"))
        if str(b.get("guild_id") or "").isdigit():
            st["guild_id"] = int(b["guild_id"])
        if "owner_ids" in b:
            st["owner_ids"] = [int(x) for x in str(b.get("owner_ids") or "").replace(" ", "").split(",") if x.isdigit()] or st.get("owner_ids", [])
        if "public_url" in b:
            st["public_url"] = (b.get("public_url") or "").strip().rstrip("/")
        self.state.save()
        await self.state.start_bot()
        return web.json_response({"ok": True, "invite": self.invite_url(), "bot_name": st.get("bot_name")})

    async def api_setup_restart(self, request):
        b = await self.body(request)
        if not await self.setup_allowed(request, b.get("code")):
            raise EconError("Not allowed.")
        await self.state.start_bot()
        return web.json_response({"ok": True})

    # ───────── helpers ─────────
    def _cit(self, r):
        return {"id": r["id"], "dept": r.get("dept"), "amount": r["amount"], "late_fee": r["late_fee"],
                "reason": r["reason"], "status": r["status"], "issued_at": _iso(r["issued_at"]),
                "due_at": _iso(r["due_at"]), "paid_at": _iso(r["paid_at"]), "note": r["note"],
                "user_id": str(r["user_id"]), "overdue": bool(r["status"] == "unpaid" and r["due_at"]
                                                              and r["due_at"] < dt.datetime.utcnow())}

    async def _ledger_rows(self, rows):
        orgs = {o["id"]: o for o in await q("SELECT id,name,kind FROM orgs WHERE guild_id=%s", self.gid)}

        async def lab(t, i):
            if t in ("wallet", "bank"):
                w = await self.who(i)
                return {"type": t, "id": str(i), "label": w["name"], "avatar": w["avatar"]}
            if t == "org":
                o = orgs.get(i)
                return {"type": o["kind"] if o else "org", "id": str(i), "label": o["name"] if o else f"(deleted #{i})"}
            return {"type": t, "id": "0", "label": t}
        return [{"id": r["id"], "ts": _iso(r["ts"]), "kind": r["kind"], "amount": r["amount"], "memo": r["memo"],
                 "actor": str(r["actor_id"]) if r["actor_id"] else None, "reversed_by": r["reversed_by"],
                 "src": await lab(r["src_type"], r["src_id"]), "dst": await lab(r["dst_type"], r["dst_id"])} for r in rows]


def _iso(d):
    return d.replace(tzinfo=dt.timezone.utc).isoformat() if d else None


def _dumps(o):
    return json.dumps(o, default=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x))
