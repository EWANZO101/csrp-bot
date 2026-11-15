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
import asyncio, hashlib, json, logging, os, re
from datetime import datetime, timedelta, timezone

import aiohttp

import discord
from discord.ext import commands, tasks

import core
from core import q, q1, ex, transfer, ensure_user, emb, MINT, BURN, GOLD, GREEN, RED

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


# The 3 s whole-channel sweep is temporary: it switches itself off at the end of 22 Nov 2026 (UTC).
POLL_UNTIL = datetime(2026, 11, 23, tzinfo=timezone.utc)

# Self-cleanup: once a message's data is fully imported, delete it from the channel.
# Wait this long after the last post/edit (the old bot edits cards, e.g. inventory pages) before deleting.
CLEANUP_GRACE = timedelta(minutes=10)
GOOD_KINDS = {"balance", "inventory", "org_inventory", "org_list"}   # anything else (unknown, inventory?, error) is kept


class Importer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = core.IMPORT_LOCK   # shared with character switching
        self.backfilled = False
        self.img_busy = set()          # (msg_id, idx) currently being downloaded / OCR'd
        self.warned_perm = False
        self.poll.start()
        self.cleanup.start()

    def cog_unload(self):
        self.poll.cancel()
        self.cleanup.cancel()

    # ───────── images (saved for review; data is read off them by staff/Claude and imported via tools/import_manual.py) ─────────
    async def cog_load(self):
        await ex("""CREATE TABLE IF NOT EXISTS import_images (
          msg_id BIGINT UNSIGNED NOT NULL, idx INT NOT NULL, guild_id BIGINT UNSIGNED NOT NULL,
          author_id BIGINT UNSIGNED NULL, filename VARCHAR(200) NOT NULL, path VARCHAR(300) NOT NULL, url VARCHAR(600) NULL,
          status VARCHAR(12) NOT NULL DEFAULT 'pending', note VARCHAR(500) NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (msg_id, idx)) ENGINE=InnoDB""")

    @staticmethod
    def image_list(msg):
        found = [(a.filename, a.url) for a in msg.attachments
                 if (a.content_type or "").startswith("image/") or a.filename.lower().rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "webp", "gif")]
        for e in msg.embeds:
            for media in (e.image, e.thumbnail):
                url = getattr(media, "url", None)
                if url and "/avatars/" not in url and "/icons/" not in url:
                    found.append((url.split("?")[0].rsplit("/", 1)[-1] or "embed.png", url))
        return found

    async def capture_images(self, msg) -> int:
        ch_id, _ = self.conf
        if not (ch_id and msg.channel.id == ch_id and msg.guild):
            return 0
        found = self.image_list(msg)
        saved = 0
        folder = os.path.join(os.getenv("DATA_DIR", "/app/data"), "import-images")
        for idx, (fname, url) in enumerate(found):
            key = (msg.id, idx)
            if key in self.img_busy:      # live event and 3 s sweep can see the same image at once
                continue
            if await q1("SELECT 1 FROM import_images WHERE msg_id=%s AND idx=%s", msg.id, idx):
                continue
            self.img_busy.add(key)
            try:
                saved += await self._save_and_read(msg, idx, fname, url, folder)
            finally:
                self.img_busy.discard(key)
        return saved

    async def _save_and_read(self, msg, idx, fname, url, folder) -> int:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", fname)[-80:] or "image.png"
        path = os.path.join(folder, f"{msg.id}_{idx}_{safe}")
        try:
            os.makedirs(folder, exist_ok=True)
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        return 0
                    data = await r.read()
            with open(path, "wb") as f:
                f.write(data)
        except Exception:
            logger.exception("couldn't save image from message %s", msg.id)
            return 0
        await ex("INSERT IGNORE INTO import_images (msg_id,idx,guild_id,author_id,filename,path,url) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 msg.id, idx, msg.guild.id, msg.author.id, fname[:200], path, url[:600])
        logger.info("saved image: %s (message %s by %s)", path, msg.id, msg.author)
        await self.auto_import(msg, idx, path)
        return 1

    # ───────── screenshots → OCR → importer (same snapshot logic, so nothing doubles) ─────────
    @staticmethod
    def _ocr(path):
        import pytesseract
        from PIL import Image, ImageOps, ImageStat
        im = Image.open(path).convert("L")
        if ImageStat.Stat(im).mean[0] < 128:          # Discord dark theme → invert to dark-on-light
            im = ImageOps.invert(im)
        im = im.resize((im.width * 2, im.height * 2))
        return pytesseract.image_to_string(im)

    def _resolve(self, guild, name, poster):
        n = (name or "").strip().lstrip("@").lower()
        if not n:
            return None
        def names(m):
            return {x.lower() for x in (m.name, m.display_name, getattr(m, "global_name", None) or "", getattr(m, "nick", None) or "") if x}
        if poster is not None and n in names(poster):
            return poster.id
        hits = [m for m in guild.members if n in names(m)]
        return hits[0].id if len(hits) == 1 else None

    def _card(self, guild, data, uid, poster):
        k = data["kind"]
        if k == "balance":
            e = discord.Embed(title="💰 Account Balance", description=f"Showing balances for <@{uid}>")
            e.add_field(name="💵 Wallet", value=f"${data['wallet']:,}.00")
            e.add_field(name="🏦 Bank", value=f"${data['bank']:,}.00")
            if data["businesses"]:
                e.add_field(name="🏢 Businesses", value="\n".join(f"**{n}** - ${v:,}.00" for n, v in data["businesses"])[:1024], inline=False)
            if data["departments"]:
                e.add_field(name="🏛️ Departments", value="\n".join(f"**{n}** - ${v:,}.00" for n, v in data["departments"])[:1024], inline=False)
            return e
        if k == "inventory":
            e = discord.Embed(title=f"🎒 Inventory for {data['name']}",
                              description=f"<@{uid}>\n" + "\n".join(f"• {i}" for i in data["items"]))
            e.set_footer(text=f"Page {data['page']} / ?")
            return e
        if k == "org_inventory":
            return discord.Embed(title=f"📦 Inventory — {data['org']}", description="\n".join(
                f"**{n}** — Price: `${p}.0` | Stock: `{'∞' if s is None else s}`" for n, p, s in data["items"]))
        if k == "org_list":
            rows = []
            for n, owner, bal in data["rows"]:
                oid = self._resolve(guild, owner, poster)
                rows.append(f"**{n}** — " + (f"Owned by <@{oid}> | " if oid else "") + f"Balance: `${bal}.0`")
            return discord.Embed(title="📋 Business List", description="\n".join(rows))
        return None

    async def auto_import(self, msg, idx, path):
        from cogs.ocr_parse import parse
        gid, ch_id = msg.guild.id, self.conf[0]
        async def done(status, note, title, color, body=""):
            await ex("UPDATE import_images SET status=%s, note=%s WHERE msg_id=%s AND idx=%s", status, note[:500], msg.id, idx)
            await core.log(self.bot, gid, emb(title, (body + f"\n[screenshot]({msg.jump_url}) from {msg.author.mention}")[:4000], color))
        try:
            text = await asyncio.get_running_loop().run_in_executor(None, self._ocr, path)
        except Exception as err:
            logger.exception("OCR failed for %s", path)
            return await done("review", f"OCR failed: {err}", "📷 Screenshot needs review", GOLD, "Couldn't read the image.")
        data = parse(text)
        if not data:
            return await done("review", "not a recognised old-bot card", "📷 Screenshot needs review", GOLD,
                              "Saved, but it doesn't look like an old-bot balance / inventory / stock card.")
        uid = None
        if data["kind"] in ("balance", "inventory"):
            uid = self._resolve(msg.guild, data["name"], msg.author)
            if not uid:
                return await done("review", f"couldn't match player '{data['name']}'", "📷 Screenshot needs review", GOLD,
                                  f"Read a **{data['kind']}** card for **{data['name']}**, but couldn't tell which member that is.")
        card = self._card(msg.guild, data, uid, msg.author)
        async with self.lock:
            try:
                kind, changes = await self.apply_embed(gid, msg, card)
            except Exception as err:
                logger.exception("screenshot import failed for %s", path)
                return await done("review", f"import error: {err}", "📷 Screenshot needs review", RED, "Import failed — see logs.")
        logger.info("screenshot %s imported (%s): %s", path, kind, "; ".join(changes) or "no changes")
        await done("imported", "; ".join(changes) or "no changes (already up to date)",
                   "📷 Screenshot imported automatically", GREEN,
                   "\n".join(f"• {c}" for c in changes)[:3500] or "Already up to date — nothing changed.")

    async def retry_pending_images(self):
        ch = self.bot.get_channel(self.conf[0])
        if not ch:
            return
        for r in await q("SELECT * FROM import_images WHERE status='pending' AND created_at < NOW() - INTERVAL 1 MINUTE ORDER BY msg_id"):
            key = (r["msg_id"], r["idx"])
            if key in self.img_busy:
                continue
            try:
                m = await ch.fetch_message(r["msg_id"])
            except discord.HTTPException:
                continue
            self.img_busy.add(key)
            try:
                await self.auto_import(m, r["idx"], r["path"])
            finally:
                self.img_busy.discard(key)

    @tasks.loop(seconds=3)
    async def poll(self):
        """Every 3 s: snapshot the WHOLE import channel and import anything new or edited.
        process()/capture_images() are idempotent (hash + DB checks), so already-imported messages are skipped.
        Runs only until POLL_UNTIL; after that the loop stops itself (live message/edit events keep importing)."""
        if datetime.now(timezone.utc) >= POLL_UNTIL:
            logger.info("poll: 3 s channel sweep expired (%s) — disabled", POLL_UNTIL.isoformat())
            self.poll.cancel()
            return
        if not self.backfilled:
            return
        ch_id, _ = self.conf
        ch = self.bot.get_channel(ch_id) if ch_id else None
        if not ch:
            return
        n = 0
        try:
            async for msg in ch.history(limit=None, oldest_first=True):
                try:
                    await self.capture_images(msg)
                    if await self.process(msg):
                        n += 1
                except Exception:
                    logger.exception("poll: failed on message %s", msg.id)
        except discord.HTTPException as e:
            logger.warning("poll: couldn't read import channel %s: %s", ch_id, e)
        except Exception:
            logger.exception("poll: channel sweep failed")   # never let the loop die
        try:
            await self.retry_pending_images()     # screenshots saved but never read (e.g. bot restarted mid-OCR)
        except Exception:
            logger.exception("poll: retrying pending screenshots failed")
        if n:
            logger.info("poll: %d new/changed message(s) imported from #%s", n, ch)

    @poll.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ───────── self-cleanup: delete messages whose data is safely in the DB ─────────
    @tasks.loop(seconds=60)
    async def cleanup(self):
        if not self.backfilled:
            return
        try:
            await self._cleanup()
        except Exception:
            logger.exception("cleanup: sweep failed")

    @cleanup.before_loop
    async def _wait_cleanup(self):
        await self.bot.wait_until_ready()

    async def _cleanup(self):
        ch_id, _ = self.conf
        ch = self.bot.get_channel(ch_id) if ch_id else None
        if not ch or not ch.guild:
            return
        if not ch.permissions_for(ch.guild.me).manage_messages:
            if not self.warned_perm:
                logger.warning("cleanup: bot lacks Manage Messages in #%s — imported messages are NOT being deleted", ch)
                self.warned_perm = True
            return
        self.warned_perm = False
        cutoff = datetime.now(timezone.utc) - CLEANUP_GRACE
        deleted = 0
        async for msg in ch.history(limit=None, before=cutoff, oldest_first=True):
            if msg.pinned or (msg.edited_at and msg.edited_at > cutoff):
                continue
            if not await self.fully_imported(msg):
                continue
            try:
                await msg.delete()
                deleted += 1
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                logger.warning("cleanup: couldn't delete message %s: %s", msg.id, e)
                break
        if deleted:
            logger.info("cleanup: deleted %d fully-imported message(s) from #%s", deleted, ch)

    async def fully_imported(self, msg) -> bool:
        """True only if everything in the message was imported successfully (data kept in import_log / import_images)."""
        has_data = False
        if self.wanted(msg):
            row = await q1("SELECT kind, hash FROM import_log WHERE msg_id=%s", msg.id)
            digest = hashlib.sha1(json.dumps([e.to_dict() for e in msg.embeds], sort_keys=True).encode()).hexdigest()
            if not row or row["hash"] not in (digest, None):
                return False                      # not imported yet, or changed since
            kinds = set(filter(None, (row["kind"] or "").split(",")))
            if not kinds or not kinds <= GOOD_KINDS:
                return False                      # unreadable card → keep for staff
            has_data = True
        imgs = self.image_list(msg)
        if imgs:
            rows = await q("SELECT status FROM import_images WHERE msg_id=%s", msg.id)
            if len(rows) < len(imgs) or any(r["status"] != "imported" for r in rows):
                return False                      # screenshot pending / needs review → keep
            has_data = True
        return has_data                           # plain chat / unrelated messages are never deleted

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
                await self.capture_images(msg)
                if await self.process(msg):
                    n += 1
        except discord.Forbidden:
            logger.warning("no permission to read history of import channel %s", ch_id)
        logger.info("import back-fill done: %d new message(s) applied from #%s", n, ch)
        try:
            await self.retry_pending_images()
        except Exception:
            logger.exception("retrying pending screenshots failed")

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
        await self.capture_images(msg)
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
        if ref[0] in ("wallet", "bank"):
            cid = await core.import_character(gid, ref[1])   # imports belong to the main character, even when parked
            if cid:
                ref = ("c" + ref[0], cid)
        legs = [(MINT, ref, delta)] if delta > 0 else [(ref, BURN, -delta)]
        await transfer(gid, legs, self.bot.user.id, "import", memo, allow_negative=True)

    async def cur_user(self, gid, uid, where):
        cid = await core.import_character(gid, uid)
        if cid:
            r = await q1("SELECT wallet, bank FROM characters WHERE id=%s", cid)
            return r[where] if r else 0
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
        cid = await core.import_character(gid, uid)
        if cid:
            r = await q1("SELECT qty FROM character_items WHERE char_id=%s AND name=%s", cid, name)
        else:
            r = await q1("SELECT qty FROM user_items WHERE guild_id=%s AND user_id=%s AND name=%s", gid, uid, name)
        return r["qty"] if r else 0

    async def add_item(self, gid, uid, name, delta):
        await ensure_user(gid, uid)
        cid = await core.import_character(gid, uid)   # imports belong to the main character, even when parked
        if cid:
            await ex("INSERT INTO character_items (char_id,name,source,qty) VALUES (%s,%s,'old bot',GREATEST(%s,0)) "
                     "ON DUPLICATE KEY UPDATE qty=GREATEST(qty+%s,0)", cid, name, delta, delta)
            return
        await ex("INSERT INTO user_items (guild_id,user_id,name,source,qty) VALUES (%s,%s,%s,'old bot',GREATEST(%s,0)) "
                 "ON DUPLICATE KEY UPDATE qty=GREATEST(qty+%s,0)", gid, uid, name, delta, delta)


async def setup(bot):
    await bot.add_cog(Importer(bot))
