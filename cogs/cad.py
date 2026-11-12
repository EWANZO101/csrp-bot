"""SnailyCAD (EWANZO101/snailycad-mysql) integration.

Reads the CAD's MySQL/MariaDB database directly:
  • CAD tickets (Record type TICKET, published, unpaid) for citizens whose CAD user has a linked Discord account
    are imported as economy citations automatically — fine = Σ violation.fine × counts.
  • Paying the citation in Discord / web panel marks the CAD record PAID.
  • Tickets marked paid or deleted in the CAD are voided in the economy.
  • /cad profile shows a user's CAD citizens, records and active warrants.
Configured from the web panel (Settings → SnailyCAD). Needs SELECT on the CAD DB, plus UPDATE on `Record` to mark paid.
"""
import asyncio, logging, re, datetime as dt
import aiomysql
import discord
from discord import app_commands
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, money, emb, ts, ensure_user, is_admin, log, dm, reply, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.cad")

TICKETS_SQL = """
SELECT r.id, r.caseNumber, r.createdAt, r.notes, u.discordId discord_id, ou.discordId officer_discord,
       c.name, c.surname,
       COALESCE(SUM(v.fine * COALESCE(NULLIF(v.counts, 0), 1)), 0) total,
       GROUP_CONCAT(DISTINCT pc.title ORDER BY pc.title SEPARATOR ', ') charges
FROM `Record` r
JOIN `Citizen` c ON c.id = r.citizenId
JOIN `User` u ON u.id = c.userId
LEFT JOIN `Officer` o ON o.id = r.officerId
LEFT JOIN `User` ou ON ou.id = o.userId
LEFT JOIN `_RecordToViolation` rv ON rv.A = r.id
LEFT JOIN `Violation` v ON v.id = rv.B
LEFT JOIN `PenalCode` pc ON pc.id = v.penalCodeId
WHERE r.type = 'TICKET' AND r.publishStatus = 'PUBLISHED'
  AND (r.paymentStatus IS NULL OR r.paymentStatus = 'UNPAID')
  AND u.discordId IS NOT NULL AND u.discordId <> ''
  AND r.createdAt >= %s
GROUP BY r.id
ORDER BY r.createdAt
LIMIT 200
"""


def cad_settings(bot) -> dict:
    return bot.state.settings.get("cad") or {}


def api_base(url: str) -> str:
    """Accepts the API URL, or the CAD's own URL (https://x.snailyapp.com → https://x-api.snailyapp.com/v1)."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not re.match(r"^https?://", u):
        u = "https://" + u
    m = re.match(r"^(https?://)([a-z0-9-]+)\.snailyapp\.com(/.*)?$", u, re.I)
    if m and not m.group(2).endswith("-api"):
        return f"{m.group(1)}{m.group(2)}-api.snailyapp.com/v1"
    return u if re.search(r"/v1$", u) else u + "/v1"


class SnailyAPI:
    """Talks to SnailyCAD's REST API with the global CAD API token (works for hosted snailyapp.com CADs)."""

    def __init__(self, s: dict):
        self.base = api_base(s.get("api_url"))
        self.token = s.get("api_token") or ""

    async def req(self, method, path, **kw):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as sess:
            async with sess.request(method, self.base + path, headers={"snaily-cad-api-token": self.token}, **kw) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = None
                if r.status == 401:
                    raise EconError("SnailyCAD refused the API token (check it's enabled in CAD Settings → API Token).")
                if r.status >= 400:
                    msg = (data or {}).get("message") if isinstance(data, dict) else None
                    raise EconError(f"SnailyCAD API error {r.status}: {msg or path}")
                return data

    async def users(self) -> list:
        d = await self.req("GET", "/admin/manage/users", params={"includeAll": "true"})
        return d.get("users", []) if isinstance(d, dict) else (d or [])

    async def citizens_with_records(self) -> list:
        d = await self.req("GET", "/admin/manage/records-logs", params={"includeAll": "true"})
        return d.get("citizens", []) if isinstance(d, dict) else (d or [])

    async def citizen_logs(self, cid) -> list:
        d = await self.req("GET", f"/admin/manage/records-logs/{cid}", params={"includeAll": "true"})
        return d.get("recordsLogs", []) if isinstance(d, dict) else (d or [])

    async def user_by_discord(self, discord_id) -> dict | None:
        try:
            return await self.req("GET", "/admin/manage/users/lookup", params={"discordId": str(discord_id), "select-citizens": "true"})
        except EconError:
            return None

    async def mark_paid(self, record_id):
        await self.req("POST", f"/records/mark-as-paid/{record_id}")


def record_total(rec) -> int:
    return int(sum((v.get("fine") or 0) * (v.get("counts") or 1) for v in rec.get("violations") or []))


async def make_pool(s: dict):
    return await aiomysql.create_pool(host=s.get("host") or "localhost", port=int(s.get("port") or 3306),
                                      user=s.get("user"), password=s.get("password") or "", db=s.get("database"),
                                      autocommit=True, charset="utf8mb4", minsize=0, maxsize=4, connect_timeout=8,
                                      init_command="SET time_zone='+00:00'")


async def test_connection(s: dict) -> dict:
    """Used by the web panel's Test button."""
    if s.get("mode") == "api":
        api = SnailyAPI(s)
        if not api.base or not api.token:
            raise EconError("Enter the API URL and API token.")
        users = await api.users()
        cits = await api.citizens_with_records()
        return {"linked_users": sum(1 for u in users if u.get("discordId")), "citizens": len(cits), "unpaid_tickets": None,
                "can_mark_paid": True, "api_base": api.base}
    pool = await make_pool(s)
    try:
        async with pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT COUNT(*) n FROM `User` WHERE discordId IS NOT NULL AND discordId <> ''")
            linked = (await cur.fetchone())["n"]
            await cur.execute("SELECT COUNT(*) n FROM `Citizen`")
            citizens = (await cur.fetchone())["n"]
            await cur.execute("SELECT COUNT(*) n FROM `Record` WHERE type='TICKET' AND (paymentStatus IS NULL OR paymentStatus='UNPAID')")
            unpaid = (await cur.fetchone())["n"]
            await cur.execute("SHOW GRANTS")
            grants = " ".join(str(list(r.values())[0]) for r in await cur.fetchall()).upper()
        can_update = "ALL PRIVILEGES" in grants or "UPDATE" in grants
        return {"linked_users": linked, "citizens": citizens, "unpaid_tickets": unpaid, "can_mark_paid": can_update}
    finally:
        pool.close()
        await pool.wait_closed()


class CAD(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pool = None
        self.last_error = None
        self.last_sync = None
        core.CITATION_PAID_HOOKS.append(self.on_citation_paid)
        self.sync_loop.start()

    def cog_unload(self):
        self.sync_loop.cancel()
        if self.on_citation_paid in core.CITATION_PAID_HOOKS:
            core.CITATION_PAID_HOOKS.remove(self.on_citation_paid)
        if self.pool:
            self.pool.close()

    @property
    def api_mode(self):
        return cad_settings(self.bot).get("mode") == "api"

    @property
    def enabled(self):
        s = cad_settings(self.bot)
        if s.get("mode") == "api":
            return bool(s.get("enabled") and s.get("api_url") and s.get("api_token"))
        return bool(s.get("enabled") and s.get("user") and s.get("database"))

    async def reconnect(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
        self.last_error = None
        if self.enabled and not self.api_mode:
            try:
                self.pool = await make_pool(cad_settings(self.bot))
            except Exception as e:
                self.last_error = f"connect failed: {e}"
                logger.warning("CAD %s", self.last_error)

    async def cq(self, sql, *args):
        if not self.pool:
            await self.reconnect()
        if not self.pool:
            raise EconError("SnailyCAD isn't connected — set it up in the web panel (Settings → SnailyCAD).")
        async with self.pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, args)
            return await cur.fetchall()

    # ───────── sync ─────────
    @tasks.loop(minutes=2)
    async def sync_loop(self):
        if not self.enabled or not cad_settings(self.bot).get("sync_tickets", True):
            return
        if self.api_mode and self.last_sync and (dt.datetime.utcnow() - self.last_sync).total_seconds() < 290:
            return   # API mode: be gentle with the hosted CAD (every ~5 min)
        try:
            await self.sync()
            self.last_error = None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.warning("CAD sync failed: %s", self.last_error)

    @sync_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    async def sync(self) -> tuple[int, int]:
        if self.api_mode:
            return await self.sync_api()
        gid = self.bot.state.guild_id
        if not gid:
            return 0, 0
        s = cad_settings(self.bot)
        c = await cfg(gid)
        since = s.get("since") or "2000-01-01 00:00:00"
        imported = 0
        for r in await self.cq(TICKETS_SQL, since):
            total = int(r["total"] or 0)
            if total <= 0 or not str(r["discord_id"]).isdigit():
                continue
            if await q1("SELECT 1 FROM citations WHERE cad_record_id=%s", r["id"]):
                continue
            uid = int(r["discord_id"])
            await ensure_user(gid, uid)
            issuer = int(r["officer_discord"]) if str(r["officer_discord"] or "").isdigit() else self.bot.user.id
            reason = f"CAD ticket #{r['caseNumber']} ({r['name']} {r['surname']})"
            if r["charges"]:
                reason += f": {r['charges']}"
            dept = s.get("dept_id") or None
            if dept and not await q1("SELECT 1 FROM orgs WHERE id=%s AND guild_id=%s AND kind='dept'", int(dept), gid):
                dept = None
            n, cid = await ex("INSERT IGNORE INTO citations (guild_id,dept_id,user_id,issuer_id,amount,reason,issued_at,due_at,cad_record_id) "
                              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s + INTERVAL %s DAY,%s)",
                              gid, dept, uid, issuer, total, reason[:500], r["createdAt"], r["createdAt"],
                              int(c["citation_days"]), r["id"])
            if not n:
                continue
            imported += 1
            e = emb(f"🚨 New citation from SnailyCAD — #{cid}", reason, RED)
            e.add_field(name="Fine", value=money(c, total))
            e.add_field(name="Pay", value=f"`/citation pay {cid}`")
            await dm(self.bot, uid, e, gid)
            await log(self.bot, gid, emb("🔗 CAD ticket imported", f"#{cid} <@{uid}> · {money(c, total)} · case {r['caseNumber']}", BLUE))
        # reconcile: tickets paid/removed in the CAD → void here
        voided = 0
        open_rows = await q("SELECT id, cad_record_id FROM citations WHERE guild_id=%s AND status='unpaid' AND cad_record_id IS NOT NULL", gid)
        for i in range(0, len(open_rows), 100):
            chunk = open_rows[i:i + 100]
            ids = [r["cad_record_id"] for r in chunk]
            found = await self.cq(f"SELECT id, paymentStatus FROM `Record` WHERE id IN ({','.join(['%s'] * len(ids))})", *ids)
            state = {f["id"]: f["paymentStatus"] for f in found}
            for r in chunk:
                st = state.get(r["cad_record_id"], "MISSING")
                if st in ("PAID", "MISSING"):
                    note = "Marked paid in SnailyCAD" if st == "PAID" else "Removed from SnailyCAD"
                    await ex("UPDATE citations SET status='void', note=%s WHERE id=%s AND status='unpaid'", note, r["id"])
                    voided += 1
        self.last_sync = dt.datetime.utcnow()
        return imported, voided

    async def import_ticket(self, gid, c, s, rec_id, case_no, created, discord_id, officer_discord, who, charges, total):
        if total <= 0 or not str(discord_id or "").isdigit():
            return False
        if await q1("SELECT 1 FROM citations WHERE cad_record_id=%s", rec_id):
            return False
        uid = int(discord_id)
        await ensure_user(gid, uid)
        issuer = int(officer_discord) if str(officer_discord or "").isdigit() else self.bot.user.id
        reason = f"CAD ticket #{case_no} ({who})" + (f": {charges}" if charges else "")
        dept = s.get("dept_id") or None
        if dept and not await q1("SELECT 1 FROM orgs WHERE id=%s AND guild_id=%s AND kind='dept'", int(dept), gid):
            dept = None
        n, cid = await ex("INSERT IGNORE INTO citations (guild_id,dept_id,user_id,issuer_id,amount,reason,issued_at,due_at,cad_record_id) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s + INTERVAL %s DAY,%s)",
                          gid, dept, uid, issuer, total, reason[:500], created, created, int(c["citation_days"]), rec_id)
        if not n:
            return False
        e = emb(f"🚨 New citation from SnailyCAD — #{cid}", reason, RED)
        e.add_field(name="Fine", value=money(c, total))
        e.add_field(name="Pay", value=f"`/citation pay {cid}`")
        await dm(self.bot, uid, e, gid)
        await log(self.bot, gid, emb("🔗 CAD ticket imported", f"#{cid} <@{uid}> · {money(c, total)} · case {case_no}", BLUE))
        return True

    async def sync_api(self) -> tuple[int, int]:
        gid = self.bot.state.guild_id
        if not gid:
            return 0, 0
        s = cad_settings(self.bot)
        api = SnailyAPI(s)
        c = await cfg(gid)
        since = dt.datetime.fromisoformat((s.get("since") or "2000-01-01 00:00:00").replace(" ", "T"))
        discord_of = {u.get("id"): u.get("discordId") for u in await api.users() if u.get("discordId")}
        seen, imported = {}, 0
        for cit in await api.citizens_with_records():
            if not discord_of.get(cit.get("userId")):
                continue
            for lg in await api.citizen_logs(cit["id"]):
                rec = lg.get("records")
                if not isinstance(rec, dict) or rec.get("type") != "TICKET":
                    continue
                seen[rec["id"]] = rec.get("paymentStatus") or "UNPAID"
                if rec.get("publishStatus", "PUBLISHED") != "PUBLISHED" or seen[rec["id"]] == "PAID":
                    continue
                created = dt.datetime.fromisoformat(str(rec.get("createdAt", "2000-01-01T00:00:00")).replace("Z", "")[:19])
                if created < since:
                    continue
                off = rec.get("officer") or {}
                charges = ", ".join(sorted({(v.get("penalCode") or {}).get("title", "") for v in rec.get("violations") or []} - {""}))
                if await self.import_ticket(gid, c, s, rec["id"], rec.get("caseNumber"), created.strftime("%Y-%m-%d %H:%M:%S"),
                                            discord_of[cit["userId"]], (off.get("user") or {}).get("discordId"),
                                            f"{cit.get('name', '')} {cit.get('surname', '')}".strip(), charges, record_total(rec)):
                    imported += 1
        voided = 0
        for r in await q("SELECT id, cad_record_id FROM citations WHERE guild_id=%s AND status='unpaid' AND cad_record_id IS NOT NULL", gid):
            if seen.get(r["cad_record_id"]) == "PAID":
                await ex("UPDATE citations SET status='void', note='Marked paid in SnailyCAD' WHERE id=%s AND status='unpaid'", r["id"])
                voided += 1
        self.last_sync = dt.datetime.utcnow()
        return imported, voided

    async def on_citation_paid(self, cid: int):
        r = await q1("SELECT cad_record_id FROM citations WHERE id=%s", cid)
        if not r or not r["cad_record_id"] or not self.enabled or not cad_settings(self.bot).get("mark_paid", True):
            return
        try:
            if self.api_mode:
                await SnailyAPI(cad_settings(self.bot)).mark_paid(r["cad_record_id"])
                return
            await self.cq("UPDATE `Record` SET paymentStatus='PAID', updatedAt=UTC_TIMESTAMP(3) WHERE id=%s", r["cad_record_id"])
        except Exception as e:
            self.last_error = f"couldn't mark CAD record paid: {e}"
            logger.warning(self.last_error)

    # ───────── commands ─────────
    cad = app_commands.Group(name="cad", description="SnailyCAD link", guild_only=True)

    @cad.command(name="profile", description="Your (or someone's) SnailyCAD citizens, records and warrants")
    async def profile(self, inter: discord.Interaction, user: discord.Member | None = None):
        user = user or inter.user
        c = await cfg(inter.guild_id)
        if user.id != inter.user.id and not is_admin(inter.user, c, "citations"):
            depts = await q("SELECT * FROM orgs WHERE guild_id=%s AND kind='dept'", inter.guild_id)
            if not any([await core.org_level(inter.user, d) >= core.MEMBER for d in depts]):
                raise EconError("Only department members and admins can look up other people.")
        await inter.response.defer(ephemeral=True)
        if self.api_mode:
            return await self.profile_api(inter, user)
        cits = await self.cq(
            "SELECT c.id, c.name, c.surname, c.dateOfBirth, c.address, c.occupation, c.phoneNumber, "
            "dl.value license, wl.value weapon "
            "FROM `Citizen` c JOIN `User` u ON u.id=c.userId "
            "LEFT JOIN `Value` dl ON dl.id=c.driversLicenseId LEFT JOIN `Value` wl ON wl.id=c.weaponLicenseId "
            "WHERE u.discordId=%s ORDER BY c.createdAt LIMIT 10", str(user.id))
        if not cits:
            raise EconError(f"{user.mention} has no SnailyCAD citizens (or hasn't linked Discord in the CAD).")
        e = emb(f"🪪 {user.display_name} — SnailyCAD", None, BLUE)
        for ci in cits:
            recs = await self.cq("SELECT type, COUNT(*) n, SUM(paymentStatus='UNPAID' OR paymentStatus IS NULL) unpaid "
                                 "FROM `Record` WHERE citizenId=%s AND publishStatus='PUBLISHED' GROUP BY type", ci["id"])
            war = await self.cq("SELECT COUNT(*) n FROM `Warrant` WHERE citizenId=%s AND status='ACTIVE'", ci["id"])
            rc = {r["type"]: r for r in recs}
            lines = [f"DOB {ci['dateOfBirth']:%Y-%m-%d}" if ci["dateOfBirth"] else "",
                     f"📍 {ci['address']}" if ci["address"] else "",
                     f"💼 {ci['occupation']}" if ci["occupation"] else "",
                     f"🚗 License: {ci['license'] or '—'} · 🔫 Weapon: {ci['weapon'] or '—'}",
                     f"🎫 Tickets {rc.get('TICKET', {}).get('n', 0)} · ⚠️ Warnings {rc.get('WRITTEN_WARNING', {}).get('n', 0)} · "
                     f"🚔 Arrests {rc.get('ARREST_REPORT', {}).get('n', 0)}",
                     f"🚨 **{war[0]['n']} active warrant(s)**" if war and war[0]["n"] else ""]
            e.add_field(name=f"{ci['name']} {ci['surname']}", value="\n".join(l for l in lines if l)[:1024], inline=False)
        url = cad_settings(self.bot).get("url")
        if url:
            e.url = url
        await inter.followup.send(embed=e, ephemeral=True)

    async def profile_api(self, inter, user):
        api = SnailyAPI(cad_settings(self.bot))
        u = await api.user_by_discord(user.id)
        cits = (u or {}).get("citizens") or []
        if not cits:
            raise EconError(f"{user.mention} has no SnailyCAD citizens (or hasn't linked Discord in the CAD).")
        e = emb(f"🪪 {user.display_name} — SnailyCAD", None, BLUE)
        for ci in cits[:10]:
            logs = await api.citizen_logs(ci["id"])
            recs = [lg.get("records") for lg in logs if isinstance(lg.get("records"), dict)]
            wars = [lg.get("warrant") for lg in logs if isinstance(lg.get("warrant"), dict) and lg["warrant"].get("status") == "ACTIVE"]
            cnt = lambda t: sum(1 for r in recs if r.get("type") == t)
            lines = [f"DOB {str(ci.get('dateOfBirth', ''))[:10]}" if ci.get("dateOfBirth") else "",
                     f"📍 {ci['address']}" if ci.get("address") else "", f"💼 {ci['occupation']}" if ci.get("occupation") else "",
                     f"🚗 License: {(ci.get('driversLicense') or {}).get('value', '—')} · 🔫 Weapon: {(ci.get('weaponLicense') or {}).get('value', '—')}",
                     f"🎫 Tickets {cnt('TICKET')} · ⚠️ Warnings {cnt('WRITTEN_WARNING')} · 🚔 Arrests {cnt('ARREST_REPORT')}",
                     f"🚨 **{len(wars)} active warrant(s)**" if wars else ""]
            e.add_field(name=f"{ci.get('name', '')} {ci.get('surname', '')}", value="\n".join(l for l in lines if l)[:1024], inline=False)
        await inter.followup.send(embed=e, ephemeral=True)

    @cad.command(name="sync", description="Pull new tickets from SnailyCAD now (admin)")
    async def sync_cmd(self, inter: discord.Interaction):
        await core.require_admin(inter, "setup")
        await inter.response.defer(ephemeral=True)
        imported, voided = await self.sync()
        await inter.followup.send(embed=emb("🔗 CAD sync", f"Imported **{imported}** ticket(s), voided **{voided}** "
                                            "paid/removed in the CAD.", GREEN), ephemeral=True)


async def setup(bot):
    cog = CAD(bot)
    await bot.add_cog(cog)
    if cog.enabled:
        asyncio.create_task(cog.reconnect())
