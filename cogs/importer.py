"""Import balances / org accounts / inventories posted by the old economy bot into this bot's database.

Watches one channel (settings.json → "import": {"channel_id": …, "bot_id": …}). Every embed the old bot posts there
(e.g. its "💰 Account Balance" card) is parsed and applied:
  • 💵 Wallet / 🏦 Bank           → that player's wallet / bank
  • 🏢 Businesses / 🏛️ Departments → org balances (created if missing; businesses owned by that player)
  • embeds titled "…Inventory…"   → that player's item quantities (best effort)
Values are snapshots: the first one sets the account to exactly that value; later snapshots apply only the change
since the previous snapshot, so money earned/spent in this bot meanwhile is kept. Every change is a ledger entry
(kind "import"), so it shows in history and can be undone. History is back-filled on startup.
"""
import asyncio, hashlib, json, logging, re

import discord
from discord.ext import commands, tasks

import core
from core import q, q1, ex, transfer, ensure_user, emb, MINT, BURN, GOLD

logger = logging.getLogger("economy.import")

MENTION = re.compile(r"<@!?(\d{15,21})>")
MONEY = re.compile(r"(-?)\s*\$?\s*(-?)([\d,]+)(?:\.\d+)?")
ORG_LINE = re.compile(r"^\s*(?:[-•*]\s*)?\*\*(.+?)\*\*\s*[-–—:]\s*(.+?)\s*$")
LIST_LINE = re.compile(r"^\s*(?:[-•*]\s*)?\*\*(.+?)\*\*\s*[-–—:]\s*(?:Owned by\s*<@!?(\d{15,21})>\s*\|\s*)?Balance:\s*`?\s*(-?\$?\s*-?[\d,]+(?:\.\d+)?)`?", re.I)
STOCK_LINE = re.compile(r"^\s*(?:[-•*]\s*)?\*\*(.+?)\*\*\s*[-–—:]\s*Price:\s*`?\s*(-?\$?\s*[\d,]+(?:\.\d+)?)`?\s*\|\s*Stock:\s*`?\s*([\d,]+|∞|unlimited|none|null)`?", re.I)
ORG_INV_TITLE = re.compile(r"inventory\s*[-–—:|]\s*(.+)$", re.I)
ITEM_PATTERNS = [
    re.compile(r"^(?P<q>\d+)\s*[x×]\s+(?P<n>.+)$", re.I),
    re.compile(r"^(?P<n>.+?)\s+(?:[x×]\s*(?P<q>\d+)|\(\s*[x×]?\s*(?P<q2>\d+)\s*\)|[-–—]\s+[x×]?\s*(?P<q3>\d+))$", re.I),
    re.compile(r"^(?P<n>.+?)\s*:\s*[x×]?\s*(?P<q>\d+)$", re.I),
]


def parse_money(s: str) -> int | None:
    m = MONEY.search(s or "")
    if not m:
        return None
    n = int(m.group(3).replace(",", "") or 0)
    return -n if (m.group(1) or m.group(2)) else n


def clean(s: str) -> str:
    return re.sub(r"[*_`~]", "", s or "").strip()


def parse_item_line(line: str):
    line = clean(re.sub(r"^\s*[-•*]\s*", "", line))
    if not line:
        return None
    for p in ITEM_PATTERNS:
        m = p.match(line)
        if m:
            qty = next(int(v) for k, v in m.groupdict().items() if k.startswith("q") and v)
            return clean(m.group("n"))[:64], qty
    return line[:64], 1   # plain bullet = one of that item


class Importer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.backfilled = False
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    @tasks.loop(seconds=3)
    async def poll(self):
        """Safety net: re-check the newest messages every 3 s in case a gateway event was missed."""
        if not self.backfilled:
            return
        ch_id, _ = self.conf
        ch = self.bot.get_channel(ch_id) if ch_id else None
        if not ch:
            return
        try:
            msgs = [m async for m in ch.history(limit=50)]
        except discord.HTTPException:
            return
        for msg in reversed(msgs):
            await self.process(msg)

    @poll.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ───────── config ─────────
    @property
    def conf(self):
        c = self.bot.state.settings.get("import") or {}
        return int(c.get("channel_id") or 0), int(c.get("bot_id") or 0)

    def wanted(self, msg: discord.Message) -> bool:
        ch, src = self.conf
        return bool(ch and msg.channel.id == ch and msg.guild and msg.guild.id in self.bot.guild_ids
                    and (not src or msg.author.id == src) and msg.embeds)

    # ───────── events ─────────
    @commands.Cog.listener()
    async def on_ready(self):
        if self.backfilled:
            return
        self.backfilled = True
        try:
            await self.sync_members()
        except Exception:
            logger.exception("member sync failed")
        ch_id, _ = self.conf
        ch = self.bot.get_channel(ch_id) if ch_id else None
        if not ch:
            if ch_id:
                logger.warning("import channel %s not found / not visible", ch_id)
            return
        await ex("DELETE FROM import_log WHERE kind IN ('unknown','inventory?')")  # retry with the current parsers
        n = 0
        try:
            async for msg in ch.history(limit=None, oldest_first=True):
                if await self.process(msg):
                    n += 1
        except discord.Forbidden:
            logger.warning("no permission to read history of import channel %s", ch_id)
        logger.info("import back-fill done: %d new message(s) applied from #%s", n, ch)

    async def sync_members(self):
        """Give every (human) server member an economy account."""
        new = 0
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            if not g.chunked:
                try:
                    await g.chunk()
                except discord.HTTPException:
                    pass
            for m in g.members:
                if not m.bot and await ensure_user(gid, m.id):
                    new += 1
            logger.info("member sync: %d members in %s, %d new account(s) created", len(g.members), g.name, new)
        return new

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot and member.guild.id in self.bot.guild_ids:
            if await ensure_user(member.guild.id, member.id):
                logger.info("member sync: account created for new member %s (%s)", member, member.id)

    @commands.Cog.listener()
    async def on_message(self, msg):
        await self.process(msg)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        ch_id, _ = self.conf
        if payload.channel_id != ch_id:
            return
        ch = self.bot.get_channel(ch_id)
        try:
            msg = await ch.fetch_message(payload.message_id)
        except (discord.HTTPException, AttributeError):
            return
        await self.process(msg)   # deferred replies arrive empty first, then get edited with the embed

    # ───────── core ─────────
    async def process(self, msg: discord.Message) -> bool:
        if not self.wanted(msg):
            return False
        async with self.lock:
            digest = hashlib.sha1(json.dumps([e.to_dict() for e in msg.embeds], sort_keys=True).encode()).hexdigest()
            row = await q1("SELECT hash FROM import_log WHERE msg_id=%s", msg.id)
            if row and row["hash"] in (digest, None):
                return False
            gid = msg.guild.id
            changes, kinds = [], set()
            for e in msg.embeds:
                try:
                    kind, ch = await self.apply_embed(gid, msg, e)
                except Exception:
                    logger.exception("import failed for message %s", msg.id)
                    kind, ch = "error", []
                kinds.add(kind)
                changes += ch
            if "error" in kinds:
                return False   # leave it un-logged so it is retried on the next start
            await ex("INSERT INTO import_log (msg_id,guild_id,channel_id,kind,summary,raw,hash) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                     "ON DUPLICATE KEY UPDATE kind=VALUES(kind), summary=CONCAT_WS(' | ', summary, VALUES(summary)), "
                     "raw=VALUES(raw), hash=VALUES(hash)",
                     msg.id, gid, msg.channel.id, ",".join(sorted(kinds))[:32], "; ".join(changes)[:500],
                     json.dumps([e.to_dict() for e in msg.embeds])[:1_000_000], digest)
        if changes:
            logger.info("imported message %s: %s", msg.id, "; ".join(changes))
            await core.log(self.bot, gid, emb("📥 Imported from old bot", "\n".join(f"• {c}" for c in changes)[:3800]
                                              + f"\n[source]({msg.jump_url})", GOLD))
        return True

    def inventory_owner(self, msg, e) -> int | None:
        m = re.search(r"inventory\s+(?:for|of)\s+(.+)$", clean(e.title), re.I) or \
            re.match(r"(.+?)['’]s\s+inventory", clean(e.title), re.I)
        if not m:
            return None
        name = m.group(1).strip().lstrip("@").lower()
        meta = getattr(msg, "interaction_metadata", None) or getattr(msg, "interaction", None)
        iu = getattr(meta, "user", None)
        if iu and name in {(iu.name or "").lower(), (iu.global_name or "").lower(), (getattr(iu, "display_name", "") or "").lower()}:
            return iu.id
        for mem in msg.guild.members:
            if name in {mem.name.lower(), (mem.global_name or "").lower(), mem.display_name.lower()}:
                return mem.id
        return None

    def target_user(self, msg, e) -> int | None:
        for text in (e.description, e.title, *(f.value for f in e.fields)):
            m = MENTION.search(text or "")
            if m:
                return int(m.group(1))
        meta = getattr(msg, "interaction_metadata", None) or getattr(msg, "interaction", None)
        return meta.user.id if meta and getattr(meta, "user", None) else None

    async def apply_embed(self, gid, msg, e: discord.Embed):
        title = (e.title or "").lower()
        uid = self.target_user(msg, e)
        changes = []
        stock_lines = [m for m in map(STOCK_LINE.match, (e.description or "").splitlines()) if m]
        ot = ORG_INV_TITLE.search(clean(e.title))
        if stock_lines and ot:
            name = clean(ot.group(1))[:64]
            org = await q1("SELECT * FROM orgs WHERE guild_id=%s AND name=%s ORDER BY kind='business' DESC LIMIT 1", gid, name)
            if not org:
                oid = await self.org_id(gid, "dept" if "department" in title else "business", name, None)
                org = await q1("SELECT * FROM orgs WHERE id=%s", oid)
            for m in stock_lines:
                iname, price = clean(m.group(1))[:64], max(parse_money(m.group(2)) or 0, 0)
                raw = m.group(3).replace(",", "").lower()
                stock = int(raw) if raw.isdigit() else None
                it = await q1("SELECT * FROM org_items WHERE org_id=%s AND name=%s", org["id"], iname)
                if not it:
                    await ex("INSERT INTO org_items (org_id,name,price,stock,for_sale,consumable) VALUES (%s,%s,%s,%s,1,0)",
                             org["id"], iname, price, stock)
                    changes.append(f"🛒 {org['name']}: new item {iname} @ ${price:,} · stock {stock if stock is not None else '∞'}")
                    await ex("INSERT INTO import_snapshots (guild_id,skey,value,msg_id) VALUES (%s,%s,%s,%s) "
                             "ON DUPLICATE KEY UPDATE value=VALUES(value), msg_id=VALUES(msg_id)",
                             gid, f"s:{org['id']}:{iname.lower()}"[:160], stock if stock is not None else -1, msg.id)
                    continue
                if it["price"] != price:
                    await ex("UPDATE org_items SET price=%s WHERE id=%s", price, it["id"])
                    changes.append(f"🛒 {org['name']}: {iname} price → ${price:,}")
                if stock is None:
                    if it["stock"] is not None:
                        await ex("UPDATE org_items SET stock=NULL WHERE id=%s", it["id"])
                    continue

                async def cur_stock(it=it):
                    return it["stock"] or 0

                async def add_stock(d, it=it):
                    await ex("UPDATE org_items SET stock=GREATEST(COALESCE(stock,0)+%s,0) WHERE id=%s", d, it["id"])
                d = await self.snap(gid, f"s:{org['id']}:{iname.lower()}", stock, msg.id, cur_stock, add_stock)
                if d:
                    changes.append(f"🛒 {org['name']}: {iname} stock {d:+,} → {stock:,}")
            return "org_inventory", changes or [f"🛒 {org['name']}: stock list checked, no changes"]
        if "inventory" in title:
            uid = self.inventory_owner(msg, e) or (uid if MENTION.search(e.description or "") else None)
            if not uid:
                return "inventory?", []
            items = []
            if e.fields:
                for f in e.fields:
                    qn = re.search(r"\d+", f.value or "")
                    if clean(f.name):
                        items.append((clean(f.name)[:64], int(qn.group()) if qn else 1))
            lines = [l for l in (e.description or "").splitlines() if l.strip() and not MENTION.search(l)]
            bullets = [l for l in lines if re.match(r"^\s*[-•*▪◦]\s+", l)]
            for line in (bullets or lines):
                p = parse_item_line(line)
                if p:
                    items.append(p)
            agg = {}
            for name, qty in items:
                agg[name] = agg.get(name, 0) + qty
            pm = re.search(r"page\s*(\d+)", (e.footer.text if e.footer else "") or "", re.I)
            page = int(pm.group(1)) if pm else 1
            # one snapshot per item *per page*; the player's quantity is the sum over pages
            pre, suf = f"i:{uid}:", f":p{page}"
            prev = {r["skey"][len(pre):-len(suf)] for r in await q(
                "SELECT skey FROM import_snapshots WHERE guild_id=%s AND skey LIKE %s", gid, f"i:{uid}:%:p{page}")}
            for gone in prev - {n.lower() for n in agg}:
                agg_name = (await q1("SELECT name FROM user_items WHERE guild_id=%s AND user_id=%s AND name=%s", gid, uid, gone) or {}).get("name", gone)
                agg.setdefault(agg_name, 0)   # item moved off this page → its count on this page drops to 0
            for name, qty in agg.items():
                d = await self.snap(gid, f"i:{uid}:{name.lower()}:p{page}", qty, msg.id, lambda: self.zero(),
                                    lambda d: self.add_item(gid, uid, name, d))
                if d:
                    changes.append(f"<@{uid}> {name} {d:+,}")
            return "inventory", changes
        list_lines = [m for m in map(LIST_LINE.match, (e.description or "").splitlines()) if m]
        if list_lines and ("business" in title or "department" in title or "list" in title):
            kind = "dept" if "department" in title else "business"
            for m in list_lines:
                name, owner, val = clean(m.group(1))[:64], m.group(2), parse_money(m.group(3))
                if not name or val is None:
                    continue
                oid = await self.org_id(gid, kind, name, int(owner) if owner else None)
                if owner and kind == "business":
                    await ex("UPDATE orgs SET owner_id=%s WHERE id=%s", int(owner), oid)
                d = await self.snap(gid, f"o:{kind}:{name.lower()}", val, msg.id, lambda: self.cur_org(oid),
                                    lambda d: self.move(gid, ("org", oid), d, f"Import {name} balance from old bot"))
                changes.append(f"{'🏢' if kind == 'business' else '🏛️'} {name}" + (f" (<@{owner}>)" if owner else "")
                               + (f" {d:+,} → {val:,}" if d else f" = {val:,}"))
            return "org_list", changes
        if not e.fields:
            return "unknown", []
        for f in e.fields:
            fname = f.name.lower()
            if "wallet" in fname or "bank" in fname:
                where = "wallet" if "wallet" in fname else "bank"
                val = parse_money(f.value)
                if uid is None or val is None:
                    continue
                await ensure_user(gid, uid)
                d = await self.snap(gid, f"u:{uid}:{where}", val, msg.id, lambda: self.cur_user(gid, uid, where),
                                    lambda d: self.move(gid, (where, uid), d, f"Import {where} from old bot"))
                if d:
                    changes.append(f"<@{uid}> {where} {d:+,} → {val:,}")
            elif "business" in fname or "department" in fname:
                kind = "business" if "business" in fname else "dept"
                for line in (f.value or "").splitlines():
                    m = ORG_LINE.match(line)
                    if not m:
                        continue
                    name, val = clean(m.group(1))[:64], parse_money(m.group(2))
                    if val is None or not name:
                        continue
                    oid = await self.org_id(gid, kind, name, uid)
                    d = await self.snap(gid, f"o:{kind}:{name.lower()}", val, msg.id, lambda: self.cur_org(oid),
                                        lambda d: self.move(gid, ("org", oid), d, f"Import {name} balance from old bot"))
                    if d:
                        changes.append(f"{'🏢' if kind == 'business' else '🏛️'} {name} {d:+,} → {val:,}")
        return "balance", changes

    async def snap(self, gid, key, new, msg_id, current, apply) -> int:
        """Apply a snapshot value. Returns the delta applied (0 if none / message older than the last snapshot)."""
        row = await q1("SELECT value, msg_id FROM import_snapshots WHERE guild_id=%s AND skey=%s", gid, key[:160])
        if row and msg_id < row["msg_id"]:
            return 0
        base = row["value"] if row else await current()
        delta = new - base
        if delta:
            await apply(delta)
        await ex("INSERT INTO import_snapshots (guild_id,skey,value,msg_id) VALUES (%s,%s,%s,%s) "
                 "ON DUPLICATE KEY UPDATE value=VALUES(value), msg_id=VALUES(msg_id)", gid, key[:160], new, msg_id)
        return delta

    async def move(self, gid, ref, delta, memo):
        legs = [(MINT, ref, delta)] if delta > 0 else [(ref, BURN, -delta)]
        await transfer(gid, legs, self.bot.user.id, "import", memo, allow_negative=True)

    async def cur_user(self, gid, uid, where):
        r = await q1("SELECT wallet, bank FROM users WHERE guild_id=%s AND user_id=%s", gid, uid)
        return r[where] if r else 0

    async def cur_org(self, oid):
        r = await q1("SELECT balance FROM orgs WHERE id=%s", oid)
        return r["balance"] if r else 0

    async def org_id(self, gid, kind, name, uid):
        r = await q1("SELECT id FROM orgs WHERE guild_id=%s AND kind=%s AND name=%s", gid, kind, name)
        if r:
            return r["id"]
        await ex("INSERT IGNORE INTO orgs (guild_id,kind,name,owner_id,description) VALUES (%s,%s,%s,%s,%s)",
                 gid, kind, name, uid if kind == "business" else None, "Imported from the old economy bot")
        return (await q1("SELECT id FROM orgs WHERE guild_id=%s AND kind=%s AND name=%s", gid, kind, name))["id"]

    async def zero(self):
        return 0

    async def cur_item(self, gid, uid, name):
        r = await q1("SELECT qty FROM user_items WHERE guild_id=%s AND user_id=%s AND name=%s", gid, uid, name)
        return r["qty"] if r else 0

    async def add_item(self, gid, uid, name, delta):
        await ensure_user(gid, uid)
        await ex("INSERT INTO user_items (guild_id,user_id,name,source,qty) VALUES (%s,%s,%s,'old bot',GREATEST(%s,0)) "
                 "ON DUPLICATE KEY UPDATE qty=GREATEST(qty+%s,0)", gid, uid, name, delta, delta)


async def setup(bot):
    await bot.add_cog(Importer(bot))
