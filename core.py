"""Shared plumbing: database, config, money movement, permissions, formatting."""
import os, json, re, datetime as dt
from contextlib import asynccontextmanager

import warnings
import aiomysql
warnings.filterwarnings("ignore", module="aiomysql")
import discord

pool: aiomysql.Pool | None = None
BOT_OWNERS = {int(x) for x in os.getenv("BOT_OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()}

GREEN, RED, BLUE, GOLD, GREY = 0x2ECC71, 0xE74C3C, 0x3498DB, 0xF1C40F, 0x95A5A6


CITATION_PAID_HOOKS: list = []   # async callables(citation_id) — e.g. SnailyCAD sync


async def citation_paid(cid: int):
    for hook in list(CITATION_PAID_HOOKS):
        try:
            await hook(cid)
        except Exception as e:
            print(f"[citation hook] {e!r}")


class EconError(Exception):
    """User-facing error: message is shown to the user as-is."""


# ───────────────────────── database ─────────────────────────
async def init_db():
    global pool
    pool = await aiomysql.create_pool(
        host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"], password=os.environ["DB_PASS"], db=os.environ["DB_NAME"],
        autocommit=True, charset="utf8mb4", minsize=1, maxsize=10,
        init_command="SET time_zone='+00:00'",
    )
    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
        stmts = [s for s in f.read().split(";") if s.strip()]
    async with pool.acquire() as conn, conn.cursor() as cur:
        for s in stmts:
            await cur.execute(s)


async def q(sql, *args):
    async with pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
        await cur.execute(sql, args)
        return await cur.fetchall()


async def q1(sql, *args):
    async with pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
        await cur.execute(sql, args)
        return await cur.fetchone()


async def ex(sql, *args):
    async with pool.acquire() as conn, conn.cursor() as cur:
        n = await cur.execute(sql, args)
        return n, cur.lastrowid


@asynccontextmanager
async def tx():
    async with pool.acquire() as conn:
        await conn.begin()
        cur = await conn.cursor(aiomysql.DictCursor)
        try:
            yield cur
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await cur.close()


# ───────────────────────── config ─────────────────────────
DEFAULTS = {
    "currency": "$",
    "start_balance": 5000,
    "log_channel": None,
    "staff_ranks": [
        {"id": "admin", "name": "Admin", "color": "#f87171", "perms": ["economy", "orgs", "citations", "tickets", "applications", "documents", "setup"], "roles": [], "users": [], "assign_role": False},
        {"id": "moderator", "name": "Moderator", "color": "#a78bfa", "perms": ["citations", "tickets", "applications"], "roles": [], "users": [], "assign_role": False},
        {"id": "support", "name": "Support", "color": "#34d399", "perms": ["tickets"], "roles": [], "users": [], "assign_role": False},
    ],
    "payroll_hours": 168,          # auto-payroll interval; 0 = manual only
    "sales_tax": 0,                # % of shop sales sent to the treasury
    "treasury_dept": None,         # dept id receiving tax / business fees
    "interest": 0.0,               # % daily bank interest (0 = off)
    "late_fee": 10,                # % added once when a citation is overdue
    "citation_days": 7,            # default days to pay a citation
    "business_cost": 0,            # cost to open a business
    "max_businesses": 3,           # per user
    "dm_receipts": True,
    # ── documents ──
    "docs_channel": None,
    "docs_ack_role": None,
    "docs_message": None,
    "docs_title": "Server Documents",
    "docs_text": "Please read the documents below before attending RP. Press **I have read the documents** once you're done.",
    # ── tickets ──
    "ticket_panel_channel": None,
    "ticket_panel_message": None,
    "ticket_category": None,
    "ticket_log_channel": None,
    "ticket_staff_roles": [],
    "ticket_max_open": 2,
    "ticket_types": [
        {"name": "General Support", "emoji": "💬", "description": "Questions or help with anything"},
        {"name": "Player Report", "emoji": "🚩", "description": "Report a player or rule break"},
        {"name": "Ban Appeal", "emoji": "⚖️", "description": "Appeal a ban or punishment"},
        {"name": "Department Application", "emoji": "🚓", "description": "Join LEO / Fire / EMS / DOT"},
    ],
    "ticket_panel_title": "Support Tickets",
    "ticket_panel_text": "Need help from staff? Pick a category below and a private ticket will be opened for you.",
    # ── applications ──
    "apps_channel": None,
    "apps_message": None,
    "apps_title": "Applications",
    "apps_text": "Want to join a department or the staff team? Check the requirements and apply on our website.",
    # ── branding ──
    "brand_name": "CSRP",
    "brand_full": "California State Roleplay",
    "brand_color": "#38b6ff",
    "onboarded": False,
}
FEATURES = {
    "economy": ("Banking & money", "/bank /balance /pay /deposit /withdraw /history /leaderboard"),
    "business": ("Businesses", "/business …"),
    "departments": ("Departments", "/department …"),
    "payroll": ("Payroll", "/payroll … (auto-pay)"),
    "citations": ("Citations & fines", "/citation …"),
    "shop": ("Shops & inventory", "/shop /inventory /give /use"),
    "tickets": ("Support tickets", "/ticket … + ticket panel"),
    "documents": ("Documents", "/docs + documents panel"),
    "applications": ("Applications", "/apply + application panel, web forms with requirements"),
    "cad": ("SnailyCAD link", "/cad …"),
}
FEATURE_COMMANDS = {
    "economy": ["balance", "bank", "deposit", "withdraw", "pay", "history", "leaderboard"],
    "business": ["business"], "departments": ["department"], "payroll": ["payroll"], "citations": ["citation"],
    "shop": ["shop", "inventory", "give", "use"], "tickets": ["ticket"], "documents": ["docs", "docs_panel"], "applications": ["apply", "apply_panel"], "cad": ["cad"],
}
DEFAULT_FEATURES = {k: k != "cad" for k in FEATURES}
CONFIG_TYPES = {k: type(v) if v is not None else int for k, v in DEFAULTS.items()}
CONFIG_TYPES["interest"] = float
CONFIG_TYPES["currency"] = str
CONFIG_TYPES["dm_receipts"] = bool
for _k in ("apps_title", "apps_text", "docs_title", "docs_text", "ticket_panel_title", "ticket_panel_text", "brand_name", "brand_full", "brand_color"):
    CONFIG_TYPES[_k] = str
CONFIG_TYPES["onboarded"] = bool
_cfg_cache: dict[int, dict] = {}


async def cfg(gid: int) -> dict:
    if gid not in _cfg_cache:
        row = await q1("SELECT data FROM guild_config WHERE guild_id=%s", gid)
        data = dict(DEFAULTS)
        if row:
            data.update(json.loads(row["data"]))
        _cfg_cache[gid] = data
    return _cfg_cache[gid]


async def set_cfg(gid: int, **kw):
    c = dict(await cfg(gid))
    c.update(kw)
    await ex("INSERT INTO guild_config (guild_id,data) VALUES (%s,%s) ON DUPLICATE KEY UPDATE data=VALUES(data)",
             gid, json.dumps(c))
    _cfg_cache[gid] = c
    return c


def coerce_cfg(key: str, raw: str):
    t = CONFIG_TYPES.get(key)
    if t is None:
        raise EconError(f"Unknown setting `{key}`.")
    raw = raw.strip()
    if t is list:
        raise EconError(f"`{key}` is set from the web panel.")
    if raw.lower() in ("none", "off", "null", "") and key not in ("currency",):
        if t is str:
            return ""
        return None if DEFAULTS[key] is None else (False if t is bool else 0)
    try:
        if t is bool:
            return raw.lower() in ("1", "true", "yes", "on")
        if t is int:
            return int(re.sub(r"[<#@&>]", "", raw))
        if t is float:
            return float(raw)
        return raw[:8] if key == "currency" else raw[:2000]
    except ValueError:
        raise EconError(f"`{raw}` isn't a valid value for `{key}`.")


# ───────────────────────── formatting ─────────────────────────
def money(c: dict, n: int) -> str:
    return f"-{c['currency']}{-n:,}" if n < 0 else f"{c['currency']}{n:,}"


def emb(title=None, desc=None, color=BLUE, **fields) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color)
    for k, v in fields.items():
        e.add_field(name=k.replace("_", " "), value=v, inline=True)
    return e


def ts(d: dt.datetime | None, style="R") -> str:
    if not d:
        return "—"
    return f"<t:{int(d.replace(tzinfo=dt.timezone.utc).timestamp())}:{style}>"


_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_amount(raw: str, maximum: int | None = None) -> int:
    s = str(raw).lower().replace(",", "").replace("$", "").strip()
    if s in ("all", "max"):
        if maximum is None:
            raise EconError("`all` isn't allowed here.")
        if maximum <= 0:
            raise EconError("You don't have anything to move.")
        return maximum
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb]?)", s)
    if not m:
        raise EconError(f"`{raw}` isn't a valid amount. Try `500`, `2.5k`, `1m` or `all`.")
    n = int(float(m.group(1)) * _SUFFIX.get(m.group(2), 1))
    if n <= 0:
        raise EconError("Amount must be greater than zero.")
    if n > 1_000_000_000_000:
        raise EconError("That amount is too large.")
    return n


# ───────────────────────── money movement ─────────────────────────
MINT = ("mint", 0)
BURN = ("burn", 0)


def acct_label(ref) -> str:
    return {"wallet": "wallet", "bank": "bank", "org": "account", "mint": "mint", "burn": "burn"}[ref[0]]


async def ensure_user(gid: int, uid: int) -> bool:
    n, _ = await ex("INSERT IGNORE INTO users (guild_id,user_id) VALUES (%s,%s)", gid, uid)
    if n:
        c = await cfg(gid)
        if c["start_balance"] > 0:
            await transfer(gid, [(MINT, ("bank", uid), c["start_balance"])], uid, "start", "Starting balance")
    return bool(n)


async def transfer(gid, legs, actor_id, kind, memo="", extra=None, allow_negative=False) -> list[int]:
    """Atomically apply money legs [(src, dst, amount)]. src/dst = ('wallet'|'bank', uid) | ('org', id) | MINT | BURN.
    `extra(cur)` runs inside the same DB transaction (stock, inventory, etc.)."""
    legs = [(s, d, int(a)) for s, d, a in legs if int(a) > 0]
    c = await cfg(gid)
    ids = []
    async with tx() as cur:
        refs = sorted({r for s, d, _ in legs for r in (s, d) if r[0] in ("wallet", "bank", "org")},
                      key=lambda r: (r[0] != "org", r[1], r[0]))
        bal = {}
        for r in refs:
            if r[0] == "org":
                await cur.execute("SELECT balance FROM orgs WHERE id=%s AND guild_id=%s FOR UPDATE", (r[1], gid))
                row = await cur.fetchone()
                if not row:
                    raise EconError("That business/department no longer exists.")
                bal[r] = row["balance"]
            else:
                await cur.execute("SELECT wallet,bank,frozen FROM users WHERE guild_id=%s AND user_id=%s FOR UPDATE",
                                  (gid, r[1]))
                row = await cur.fetchone()
                if not row:
                    raise EconError(f"<@{r[1]}> doesn't have an account yet.")
                if row["frozen"] and kind not in ("admin_print", "admin_remove", "undo", "wipe") and \
                        any(s == r for s, _, _ in legs):
                    raise EconError(f"<@{r[1]}>'s account is frozen.")
                bal[r] = row[r[0]]
        for s, d, a in legs:
            if s in bal:
                if bal[s] < a and not allow_negative:
                    raise EconError(f"Insufficient funds in {acct_label(s)}: need **{money(c, a)}**, "
                                    f"only **{money(c, bal[s])}** available.")
                bal[s] -= a
            if d in bal:
                bal[d] += a
        for r, v in bal.items():
            if r[0] == "org":
                await cur.execute("UPDATE orgs SET balance=%s WHERE id=%s", (v, r[1]))
            else:
                await cur.execute(f"UPDATE users SET {r[0]}=%s WHERE guild_id=%s AND user_id=%s", (v, gid, r[1]))
        for s, d, a in legs:
            await cur.execute(
                "INSERT INTO ledger (guild_id,actor_id,kind,src_type,src_id,dst_type,dst_id,amount,memo) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (gid, actor_id, kind, s[0], s[1], d[0], d[1], a, (memo or "")[:255]))
            ids.append(cur.lastrowid)
        if extra:
            await extra(cur)
    return ids


async def get_user(gid, uid):
    await ensure_user(gid, uid)
    return await q1("SELECT * FROM users WHERE guild_id=%s AND user_id=%s", gid, uid)


# ───────────────────────── permissions ─────────────────────────
NONE, MEMBER, HEAD, ADMIN = 0, 1, 2, 3


def is_creator(member) -> bool:
    return member.id in BOT_OWNERS


STAFF_PERMS = {
    "economy": ("Citizens & money", "View/adjust balances, print/remove money, freeze accounts, undo transactions"),
    "orgs": ("Businesses & departments", "Create/delete, items, roles, payroll for any business or department"),
    "citations": ("Citations", "View all citations, edit/void/waive fees, look up CAD profiles"),
    "tickets": ("Tickets", "See, claim and close every ticket, read transcripts"),
    "applications": ("Applications", "Review applications and build application forms"),
    "documents": ("Documents", "Create, edit and delete documents, see staff-only documents"),
    "setup": ("Server setup & settings", "Channels, features, panels, branding, economy settings, bot connection"),
    "staff": ("Staff management", "Create staff ranks and choose who has them"),
}


def _ids(v):
    return {int(x) for x in (v or []) if str(x).isdigit()}


def staff_perms(member, c: dict) -> set:
    """Permissions this member has in the staff area. Owners have everything."""
    if member is None:
        return set()
    if member.id in BOT_OWNERS:
        return set(STAFF_PERMS) | {"owner"}
    roles = {r.id for r in getattr(member, "roles", [])}
    out = set()
    for rk in c.get("staff_ranks") or []:
        if member.id in _ids(rk.get("users")) or roles & _ids(rk.get("roles")):
            out |= {p for p in rk.get("perms") or [] if p in STAFF_PERMS}
    return out


def staff_ranks_of(member, c: dict) -> list[str]:
    if member is None:
        return []
    if member.id in BOT_OWNERS:
        return ["Owner"]
    roles = {r.id for r in getattr(member, "roles", [])}
    return [rk.get("name", "Staff") for rk in c.get("staff_ranks") or []
            if member.id in _ids(rk.get("users")) or roles & _ids(rk.get("roles"))]


def is_admin(member, c: dict, perm: str | None = None) -> bool:
    """Staff check. perm=None → has any staff permission; otherwise that specific permission."""
    perms = staff_perms(member, c)
    return bool(perms) if perm is None else (perm in perms)


async def org_level(member: discord.Member, org: dict, ignore_admin=False) -> int:
    c = await cfg(member.guild.id)
    if not ignore_admin and is_admin(member, c, "orgs"):
        return ADMIN
    if org.get("owner_id") == member.id:
        return HEAD
    roles = await q("SELECT role_id, level FROM org_roles WHERE org_id=%s", org["id"])
    ids = {r.id for r in member.roles}
    lvl = NONE
    for r in roles:
        if r["role_id"] in ids:
            lvl = max(lvl, HEAD if r["level"] == "head" else MEMBER)
    if lvl == NONE and org["kind"] == "business":
        if await q1("SELECT 1 FROM payroll WHERE org_id=%s AND user_id=%s", org["id"], member.id):
            lvl = MEMBER
    return lvl


async def require_level(member, org, need: int, what="do that"):
    lvl = await org_level(member, org)
    if lvl < need:
        who = {MEMBER: "a member", HEAD: "a head/owner", ADMIN: "a bot admin"}[need]
        raise EconError(f"You must be {who} of **{org['name']}** to {what}.")
    return lvl


async def require_admin(inter: discord.Interaction, perm: str = "setup"):
    c = await cfg(inter.guild_id)
    if not is_admin(inter.user, c, perm):
        raise EconError(f"You need the **{STAFF_PERMS[perm][0]}** staff permission for this. Staff ranks are managed on the web panel.")
    return c


# ───────────────────────── org lookup ─────────────────────────
KIND_LABEL = {"business": "Business", "dept": "Department"}


async def find_org(gid: int, value: str, kind: str | None = None) -> dict:
    value = (value or "").strip()
    row = None
    if value.startswith("#") and value[1:].isdigit():
        row = await q1("SELECT * FROM orgs WHERE guild_id=%s AND id=%s", gid, int(value[1:]))
    if not row:
        if kind:
            row = await q1("SELECT * FROM orgs WHERE guild_id=%s AND kind=%s AND name=%s", gid, kind, value)
        else:
            row = await q1("SELECT * FROM orgs WHERE guild_id=%s AND name=%s ORDER BY kind", gid, value)
    if not row or (kind and row["kind"] != kind):
        raise EconError(f"No {KIND_LABEL.get(kind, 'business/department').lower()} called **{value}**.")
    return row


async def log(bot, gid: int, embed: discord.Embed):
    if bot is None:
        return
    c = await cfg(gid)
    if not c.get("log_channel"):
        return
    ch = bot.get_channel(c["log_channel"])
    if ch:
        embed.timestamp = discord.utils.utcnow()
        try:
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def dm(bot, uid: int, embed: discord.Embed, gid: int | None = None):
    if bot is None or (gid is not None and not (await cfg(gid)).get("dm_receipts", True)):
        return
    try:
        u = bot.get_user(uid) or await bot.fetch_user(uid)
        await u.send(embed=embed)
    except Exception:
        pass


async def reply(inter: discord.Interaction, embed=None, content=None, ephemeral=False, view=None):
    kw = {"embed": embed, "content": content, "ephemeral": ephemeral}
    if view is not None:
        kw["view"] = view
    if inter.response.is_done():
        return await inter.followup.send(**kw)
    return await inter.response.send_message(**kw)


class Confirm(discord.ui.View):
    def __init__(self, user_id: int, label="Confirm", style=discord.ButtonStyle.danger):
        super().__init__(timeout=60)
        self.user_id, self.value = user_id, None
        self.yes.label, self.yes.style = label, style

    async def interaction_check(self, i):
        return i.user.id == self.user_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def yes(self, i, b):
        self.value = True
        await i.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, i, b):
        self.value = False
        await i.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


async def confirm(inter, text: str, label="Confirm") -> bool:
    v = Confirm(inter.user.id, label)
    await reply(inter, embed=emb("Are you sure?", text, GOLD), ephemeral=True, view=v)
    await v.wait()
    return bool(v.value)
