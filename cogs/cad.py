"""SnailyCAD (EWANZO101/snailycad-mysql) integration.

Reads the CAD's MySQL/MariaDB database directly:
  • CAD tickets (Record type TICKET, published, unpaid) for citizens whose CAD user has a linked Discord account
    are imported as economy citations automatically — fine = Σ violation.fine × counts.
  • Paying the citation in Discord / web panel marks the CAD record PAID.
  • Tickets marked paid or deleted in the CAD are voided in the economy.
  • /cad profile shows a user's CAD citizens, records and active warrants.
Configured from the web panel (Settings → SnailyCAD). Needs SELECT on the CAD DB, plus UPDATE on `Record` to mark paid.
"""
import asyncio, logging, datetime as dt
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


async def make_pool(s: dict):
    return await aiomysql.create_pool(host=s.get("host") or "localhost", port=int(s.get("port") or 3306),
                                      user=s.get("user"), password=s.get("password") or "", db=s.get("database"),
                                      autocommit=True, charset="utf8mb4", minsize=0, maxsize=4, connect_timeout=8,
                                      init_command="SET time_zone='+00:00'")


async def test_connection(s: dict) -> dict:
    """Used by the web panel's Test button."""
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
    def enabled(self):
        s = cad_settings(self.bot)
        return bool(s.get("enabled") and s.get("user") and s.get("database"))

    async def reconnect(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
        self.last_error = None
        if self.enabled:
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

    async def on_citation_paid(self, cid: int):
        r = await q1("SELECT cad_record_id FROM citations WHERE id=%s", cid)
        if not r or not r["cad_record_id"] or not self.enabled or not cad_settings(self.bot).get("mark_paid", True):
            return
        try:
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
