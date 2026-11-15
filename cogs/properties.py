"""Property registry: who owns which property.

• Imported from the server's Google Sheet (its public CSV export) — every category, property, owner, Discord name and price.
• #property-list shows every category live (🟢 available / 🔴 owned). It follows the #property-requests public/private switch.
• Property requests let players pick an AVAILABLE property; approving one marks it owned here, adds it to their inventory,
  pays San Andreas Real Estate, and writes the owner back into the Google Sheet.
• Writing to the sheet goes through a tiny Google Apps Script web app pasted into the sheet (🔗 Sheet sync in
  #property-requests-admin shows the script + steps). Rows are found by property name, so adding rows to the sheet is fine.
  Changes that couldn't be written are retried every 5 minutes.
"""
import asyncio, csv, datetime as dt, hashlib, io, json, logging, re, secrets

import aiohttp
import discord
from discord.ext import commands, tasks

import core
from core import EconError, q, q1, ex, cfg, money, emb, log, reply, is_admin, GREEN, RED, BLUE, GOLD

logger = logging.getLogger("economy.properties")
DEFAULT_SHEET = "1cevcb3TND5DFuoO-jSK9yGCuTcN4--QHo-vuljIb6OA"
EXTRA_HEADERS = {"middle-end", "warehouses & salvage yards"}   # section titles in the sheet without a trailing ':'
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS properties (
      id INT AUTO_INCREMENT PRIMARY KEY,
      guild_id BIGINT UNSIGNED NOT NULL,
      category VARCHAR(80) NOT NULL,
      name VARCHAR(120) NOT NULL,
      owner_name VARCHAR(200) NULL,
      owner_discord VARCHAR(100) NULL,
      owner_id BIGINT UNSIGNED NULL,
      price BIGINT NULL,
      picture VARCHAR(500) NULL,
      sheet_row INT NULL,
      sort INT NOT NULL DEFAULT 0,
      sheet_synced TINYINT NOT NULL DEFAULT 1,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      INDEX ix_prop (guild_id, category),
      INDEX ix_prop_owner (guild_id, owner_id)
    ) ENGINE=InnoDB""",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS grp VARCHAR(40) NULL",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS sheet_category VARCHAR(80) NULL",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS audited_at DATETIME NULL",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS audited_by BIGINT UNSIGNED NULL",
    "ALTER TABLE properties ADD COLUMN IF NOT EXISTS audited_round INT NOT NULL DEFAULT 0",
]

# Display layout: group → categories, in order. Properties are sorted into these by categorize() below.
LAYOUT = [
    ("🏠 Residential", ["Low-End Houses", "Low-End Apartments", "Mid-End Houses", "Mid-End Apartments", "High-End Houses",
                       "High-End Apartments", "Penthouses", "Mansions & Estates"]),
    ("🚗 Garages", ["Low-End Garages", "Mid-End Garages", "High-End Garages"]),
    ("🏢 Businesses", ["MC Clubhouses", "Nightclubs", "Arcades", "Agencies", "Auto Shops & Workshops", "Bail Offices",
                      "Executive Offices", "Money Fronts"]),
    ("🏭 Industrial & Storage", ["Warehouses", "Salvage Yards", "Facilities & Factories", "Hangars & Airfields", "Bunkers"]),
    ("💊 Illegal Operations", ["Drug Labs", "Counterfeit & Forgery"]),
    ("📦 Other", ["Other"]),
]
GROUP_OF = {cat: g for g, cats in LAYOUT for cat in cats}
CAT_ORDER = {cat: i for i, cat in enumerate(c for _, cats in LAYOUT for c in cats)}


def categorize(sheet_cat: str, name: str) -> str:
    """Sheet section + property name → one of the LAYOUT categories."""
    s, n = (sheet_cat or "").lower(), (name or "").lower()
    has = lambda *w: any(x in n for x in w)
    tier = "Low-End" if s.startswith("low") else "Mid-End" if s.startswith(("middle", "medium")) else \
        "High-End" if s.startswith("high") else None
    if tier and "garage" in s:
        return f"{tier} Garages"
    if tier or s.startswith("mansion"):
        if has("mansion", "farm", "estate") or s.startswith("mansion"):
            return "Mansions & Estates"
        if has("penthouse"):
            return "Penthouses"
        if re.search(r"\bapt\b|apartment|\bunit\b", n):
            return f"{tier or 'High-End'} Apartments"
        return f"{tier or 'High-End'} Houses"
    if has("clubhouse"):
        return "MC Clubhouses"
    if has("weed farm", "meth", "cocaine"):
        return "Drug Labs"
    if has("counterfeit", "counterfiet", "forgery"):
        return "Counterfeit & Forgery"
    if has("nightclub"):
        return "Nightclubs"
    if has("arcade"):
        return "Arcades"
    if has("agency") or s.startswith("agenc"):
        return "Agencies"
    if has("bail"):
        return "Bail Offices"
    if s.startswith("workshop") or has("auto shop", "customs", "workshop"):
        return "Auto Shops & Workshops"
    if s.startswith("executive") or has("ceo office", "office & garage"):
        return "Executive Offices"
    if s.startswith("money"):
        return "Money Fronts"
    if s.startswith("bunker") or has("bunker"):
        return "Bunkers"
    if has("hangar", "airfield", "runway"):
        return "Hangars & Airfields"
    if has("salvage", "scrap yard", "scrapyard"):
        return "Salvage Yards"
    if s.startswith("facilit") or has("facility", "factory"):
        return "Facilities & Factories"
    if s.startswith("warehouse"):
        return "Warehouses"
    return "Other"

APPS_SCRIPT = r"""// CSRP Economy Bot → property sheet sync. Paste into Extensions › Apps Script, then Deploy › New deployment ›
// Web app (Execute as: Me, Who has access: Anyone). Copy the web app URL into the bot (🔗 Sheet sync › Set URL).
const SECRET = "%SECRET%";
const SHEET_GID = %GID%;
const COLS = {name: 2, civilian: 4, discord: 6, price: 7};   // B, D, F, G

function doPost(e) {
  const d = JSON.parse(e.postData.contents);
  if (d.secret !== SECRET) return out({ok: false, error: "bad secret"});
  if (d.action === "ping") return out({ok: true, sheet: SpreadsheetApp.getActive().getName()});
  const sh = SpreadsheetApp.getActive().getSheets().find(s => s.getSheetId() === SHEET_GID);
  if (!sh) return out({ok: false, error: "tab not found"});
  const names = sh.getRange(1, COLS.name, sh.getLastRow(), 1).getValues().map(r => String(r[0]).trim().toLowerCase());
  const want = String(d.name).trim().toLowerCase();
  let row = (d.row && names[d.row - 1] === want) ? d.row : 0;
  if (!row) { const i = names.indexOf(want); row = i >= 0 ? i + 1 : 0; }
  if (!row) return out({ok: false, error: "property not found in sheet: " + d.name});
  sh.getRange(row, COLS.civilian).setValue(d.civilian || "");
  sh.getRange(row, COLS.discord).setValue(d.discord || "");
  if (d.price !== null && d.price !== undefined && d.price !== "") sh.getRange(row, COLS.price).setValue(d.price);
  return out({ok: true, row: row});
}
function out(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
"""


def parse_price(s):
    m = re.search(r"[\d,]+", s or "")
    return int(m.group().replace(",", "")) if m and m.group().replace(",", "") else None


def parse_sheet(text: str) -> list[dict]:
    """Rows of the sheet → properties. Column B = name, D = civilian owner, F = Discord name, G = price, H = picture."""
    out, cat, sort = [], None, 0
    for i, r in enumerate(csv.reader(io.StringIO(text)), 1):
        r = (r + [""] * 8)[:8]
        name = " ".join(r[1].split())
        if not name or name.lower() == "owned properties":
            continue
        if name.endswith(":") or name.lower() in EXTRA_HEADERS:
            cat = name.rstrip(":").strip()
            continue
        if not cat:
            continue
        sort += 1
        out.append(dict(category=cat[:80], name=name[:120], owner_name=" ".join(r[3].split())[:200] or None,
                        owner_discord=r[5].strip()[:100] or None, price=parse_price(r[6]), picture=r[7].strip()[:500] or None,
                        sheet_row=i, sort=sort))
    return out


def is_owned(p) -> bool:
    return bool(p["owner_id"] or (p["owner_name"] or "").strip() or (p["owner_discord"] or "").strip())


class Properties(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._list_hash = {}
        self.retry.start()

    async def cog_load(self):
        for s in SCHEMA:
            await ex(s)
        await self.recategorize()

    def cog_unload(self):
        self.retry.cancel()

    async def recategorize(self):
        """Apply categorize() to every property (keeps owners) — so rule changes take effect on the next start."""
        changed = 0
        for p in await q("SELECT id, category, sheet_category, name, grp FROM properties"):
            src = p["sheet_category"] or p["category"]
            cat = categorize(src, p["name"])
            if (p["category"], p["grp"], p["sheet_category"]) != (cat, GROUP_OF[cat], src):
                await ex("UPDATE properties SET category=%s, grp=%s, sheet_category=%s WHERE id=%s", cat, GROUP_OF[cat], src, p["id"])
                changed += 1
        if changed:
            logger.info("re-categorised %d properties", changed)

    @property
    def conf(self) -> dict:
        c = self.bot.state.settings.setdefault("properties", {})
        if "secret" not in c:
            c.update(secret=secrets.token_urlsafe(24), sheet_id=c.get("sheet_id") or DEFAULT_SHEET, gid=c.get("gid", 0))
            self.bot.state.save()
        return c

    # ───── data ─────
    async def count(self, gid) -> int:
        return (await q1("SELECT COUNT(*) n FROM properties WHERE guild_id=%s", gid))["n"]

    async def categories(self, gid, available_only=False, group=None):
        rows = await q("SELECT category, MAX(grp) grp, COUNT(*) n, SUM(owner_id IS NULL AND COALESCE(TRIM(owner_name),'')='' "
                       "AND COALESCE(TRIM(owner_discord),'')='') free FROM properties WHERE guild_id=%s GROUP BY category", gid)
        rows = list(rows)
        rows.sort(key=lambda r: CAT_ORDER.get(r["category"], 999))
        return [r for r in rows if (not available_only or r["free"]) and (group is None or r["grp"] == group)]

    async def groups(self, gid, available_only=False):
        out = {}
        for r in await self.categories(gid, available_only):
            g = out.setdefault(r["grp"] or "📦 Other", {"grp": r["grp"] or "📦 Other", "n": 0, "free": 0, "cats": 0})
            g["n"] += r["n"]; g["free"] += int(r["free"]); g["cats"] += 1
        return list(out.values())

    async def available(self, gid, category):
        return await q("SELECT * FROM properties WHERE guild_id=%s AND category=%s AND owner_id IS NULL "
                       "AND COALESCE(TRIM(owner_name),'')='' AND COALESCE(TRIM(owner_discord),'')='' ORDER BY sort", gid, category)

    async def get(self, pid):
        return await q1("SELECT * FROM properties WHERE id=%s", pid)

    async def real_estate_dept(self, gid):
        did = self.conf.get("dept_id")
        if did and await q1("SELECT 1 FROM orgs WHERE id=%s AND guild_id=%s", did, gid):
            return did
        r = await q1("SELECT id FROM orgs WHERE guild_id=%s AND kind='dept' AND name LIKE %s ORDER BY id LIMIT 1", gid, "%real estate%")
        return r["id"] if r else None

    def match_member(self, guild, name):
        if not name or not guild:
            return None
        n = name.strip().lstrip("@").lower()
        for m in guild.members:
            if n in {m.name.lower(), (m.global_name or "").lower(), m.display_name.lower()}:
                return m.id
        return None

    async def import_sheet(self, guild) -> dict:
        url = f"https://docs.google.com/spreadsheets/d/{self.conf['sheet_id']}/export?format=csv&gid={self.conf.get('gid', 0)}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200 or "csv" not in r.headers.get("content-type", ""):
                    raise EconError("Couldn't read the Google Sheet — make sure it's shared as 'Anyone with the link can view'.")
                text = await r.text()
        rows = parse_sheet(text)
        if not rows:
            raise EconError("The sheet didn't contain any properties I could read.")
        linked = 0
        async with core.tx() as cur:
            await cur.execute("DELETE FROM properties WHERE guild_id=%s", (guild.id,))
            for p in rows:
                oid = self.match_member(guild, p["owner_discord"])
                linked += bool(oid)
                cat = categorize(p["category"], p["name"])
                await cur.execute("INSERT INTO properties (guild_id,category,grp,sheet_category,name,owner_name,owner_discord,owner_id,"
                                  "price,picture,sheet_row,sort) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                  (guild.id, cat, GROUP_OF[cat], p["category"], p["name"], p["owner_name"], p["owner_discord"], oid,
                                   p["price"], p["picture"], p["sheet_row"], p["sort"]))
        owned = sum(1 for p in rows if p["owner_name"] or p["owner_discord"])
        self._list_hash.clear()
        return {"total": len(rows), "owned": owned, "linked": linked,
                "categories": len({categorize(p["category"], p["name"]) for p in rows})}

    async def set_owner(self, pid, owner_id, owner_name, owner_discord, price=None, cur=None) -> None:
        sql = ("UPDATE properties SET owner_id=%s, owner_name=%s, owner_discord=%s, price=COALESCE(%s, price), sheet_synced=0, "
               "updated_at=UTC_TIMESTAMP() WHERE id=%s")
        args = (owner_id, owner_name, owner_discord, price, pid)
        if cur:
            await cur.execute(sql, args)
        else:
            await ex(sql, *args)

    # ───── Google Sheet write-back ─────
    async def push(self, pid) -> tuple[bool, str]:
        p = await self.get(pid)
        if not p:
            return False, "property gone"
        url = self.conf.get("webhook")
        if not url:
            return False, "sheet sync not set up"
        body = {"secret": self.conf["secret"], "row": p["sheet_row"], "name": p["name"], "civilian": p["owner_name"] or "",
                "discord": p["owner_discord"] or "", "price": p["price"]}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    data = json.loads(await r.text())
        except Exception as err:
            return False, f"couldn't reach the sheet script ({type(err).__name__})"
        if data.get("ok"):
            await ex("UPDATE properties SET sheet_synced=1, sheet_row=COALESCE(%s, sheet_row) WHERE id=%s", data.get("row"), pid)
            return True, f"row {data.get('row')}"
        return False, data.get("error") or "unknown error"

    async def ping(self, url) -> tuple[bool, str]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"secret": self.conf["secret"], "action": "ping"},
                                  timeout=aiohttp.ClientTimeout(total=30)) as r:
                    data = json.loads(await r.text())
        except Exception as err:
            return False, f"no valid reply ({type(err).__name__}) — is it deployed as a Web app with access 'Anyone'?"
        return (True, data.get("sheet", "ok")) if data.get("ok") else (False, data.get("error") or "unknown error")

    @tasks.loop(minutes=5)
    async def retry(self):
        if not self.conf.get("webhook"):
            return
        for p in await q("SELECT id FROM properties WHERE sheet_synced=0 LIMIT 20"):
            ok, msg = await self.push(p["id"])
            if not ok:
                logger.warning("sheet sync still failing for property %s: %s", p["id"], msg)
                break

    @retry.before_loop
    async def _wr(self):
        await self.bot.wait_until_ready()

    # ───── #property-list ─────
    async def refresh_list(self, guild, ow, perms=False):
        gid = guild.id
        if not await self.count(gid):
            return
        c = await cfg(gid)
        conf = self.conf
        ch = guild.get_channel(int(conf.get("list_channel") or 0))
        if not ch:
            req = self.bot.state.settings.get("requests", {}).get("property", {})
            near = guild.get_channel(int(req.get("player_channel") or 0))
            ch = await guild.create_text_channel("property-list", overwrites=ow, category=getattr(near, "category", None),
                                                 topic="Every property and who owns it — 🟢 available · 🔴 owned")
            conf.update(list_channel=ch.id, list_messages={})
            self._list_hash.clear()
            logger.info("created #%s (%s)", ch.name, ch.id)
        elif perms:
            await ch.edit(overwrites=ow)
        msgs = conf.setdefault("list_messages", {})
        cats = await self.categories(gid)
        blocks = []   # (key, embed) in display order: a header per group, then its categories
        last_grp = None
        for cat in cats:
            if cat["grp"] != last_grp:
                last_grp = cat["grp"]
                gs = [x for x in cats if x["grp"] == last_grp]
                blocks.append((f"group:{last_grp}", discord.Embed(
                    title=f"{last_grp}", colour=0x5865F2,
                    description=f"**{sum(int(x['free']) for x in gs)}** of {sum(x['n'] for x in gs)} available · "
                                + " · ".join(x["category"] for x in gs))))
            rows = await q("SELECT * FROM properties WHERE guild_id=%s AND category=%s ORDER BY sort", gid, cat["category"])
            lines = []
            for p in rows:
                price = f" · {money(c, p['price'])}" if p["price"] else ""
                if is_owned(p):
                    who = " / ".join(x for x in (p["owner_name"], f"<@{p['owner_id']}>" if p["owner_id"] else p["owner_discord"]) if x)
                    lines.append(f"🔴 **{p['name']}** — {who}{price}")
                else:
                    lines.append(f"🟢 **{p['name']}**{price}")
            blocks.append((f"cat:{cat['category']}", discord.Embed(
                title=f"{cat['category']} — {cat['free']} of {cat['n']} available",
                colour=0x2ECC71 if cat["free"] else 0x95A5A6, description="\n".join(lines)[:4090])))
        layout = [k for k, _ in blocks]
        if conf.get("list_layout") != layout:   # new/changed categories → wipe and repost in order
            for mid in list(msgs.values()):
                try:
                    await ch.get_partial_message(int(mid)).delete()
                except discord.HTTPException:
                    pass
            msgs.clear()
            self._list_hash.clear()
            conf["list_layout"] = layout
        for key, e in blocks:
            h = hashlib.sha1(json.dumps(e.to_dict(), sort_keys=True).encode()).hexdigest()
            if self._list_hash.get(key) == h and msgs.get(key):
                continue
            done = False
            if msgs.get(key):
                try:
                    await ch.get_partial_message(int(msgs[key])).edit(embed=e, allowed_mentions=discord.AllowedMentions.none())
                    done = True
                except discord.NotFound:
                    pass
            if not done:
                m = await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
                msgs[key] = m.id
            self._list_hash[key] = h
        self.bot.state.save()


async def setup(bot):
    await bot.add_cog(Properties(bot))


# ═════════════════════ staff tools (buttons live in #property-requests-admin) ═════════════════════
class EditModal(discord.ui.Modal, title="🏘️ Edit a property"):
    prop = discord.ui.TextInput(label="Property (exact name, or #id from the list)", max_length=120, placeholder="e.g. 1893 Grapeseed Avenue")
    owner = discord.ui.TextInput(label="Owner's Discord (blank = available)", max_length=100, required=False,
                                 placeholder="@name, ID or username — leave blank to make it available")
    civ = discord.ui.TextInput(label="Owner's character name", max_length=200, required=False, placeholder="e.g. Jax Jacobs")
    price = discord.ui.TextInput(label="Price (blank = keep current)", max_length=20, required=False)

    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, inter):
        try:
            await self.cog.edit(inter, self.prop.value.strip(), self.owner.value.strip(), self.civ.value.strip(), self.price.value.strip())
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class UrlModal(discord.ui.Modal, title="🔗 Google Sheet web app URL"):
    url = discord.ui.TextInput(label="Web app URL (ends in /exec)", max_length=300, placeholder="https://script.google.com/macros/s/…/exec")

    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, inter):
        url = self.url.value.strip()
        if not re.match(r"^https://script\.google\.com/macros/s/[\w-]+/exec$", url):
            return await reply(inter, embed=emb("❌ That doesn't look like an Apps Script web app URL (…/exec).", None, RED), ephemeral=True)
        await inter.response.defer(ephemeral=True)
        ok, msg = await self.cog.ping(url)
        if not ok:
            return await inter.followup.send(embed=emb("❌ Sheet script didn't answer correctly", msg, RED), ephemeral=True)
        self.cog.conf["webhook"] = url
        self.cog.bot.state.save()
        n = (await q1("SELECT COUNT(*) n FROM properties WHERE guild_id=%s AND sheet_synced=0", inter.guild_id))["n"]
        await inter.followup.send(embed=emb("✅ Google Sheet connected", f"Linked to **{msg}**. Approved sales will now be written to the sheet."
                                            + (f"\n{n} earlier change(s) will be synced within 5 minutes." if n else ""), GREEN), ephemeral=True)
        await log(self.cog.bot, inter.guild_id, emb("🔗 Property sheet sync connected", f"by {inter.user.mention}", GOLD))


class SheetMenu(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog

    @discord.ui.button(label="Set web app URL", emoji="🔗", style=discord.ButtonStyle.primary)
    async def set_url(self, inter, b):
        await inter.response.send_modal(UrlModal(self.cog))

    @discord.ui.button(label="Push all owners to sheet now", emoji="📤", style=discord.ButtonStyle.secondary)
    async def push_all(self, inter, b):
        if not self.cog.conf.get("webhook"):
            return await reply(inter, embed=emb("❌ Set the web app URL first.", None, RED), ephemeral=True)
        await ex("UPDATE properties SET sheet_synced=0 WHERE guild_id=%s AND owner_id IS NOT NULL", inter.guild_id)
        await reply(inter, embed=emb("📤 Queued", "Every bot-linked owner will be written to the sheet over the next few minutes.", GREEN),
                    ephemeral=True)

    @discord.ui.button(label="Disconnect", emoji="✖️", style=discord.ButtonStyle.danger)
    async def disconnect(self, inter, b):
        self.cog.conf.pop("webhook", None)
        self.cog.bot.state.save()
        await reply(inter, embed=emb("✖️ Sheet sync disconnected", None, GOLD), ephemeral=True)


async def _admin(self, inter, act):
    c = await cfg(inter.guild_id)
    if act == "audit":
        if not await self.count(inter.guild_id):
            raise EconError("No properties yet — use 📥 Import from sheet first.")
        return await audit_start(self, inter)
    if act == "edit":
        if not await self.count(inter.guild_id):
            raise EconError("No properties yet — use 📥 Import from sheet first.")
        return await inter.response.send_modal(EditModal(self))
    if not is_admin(inter.user, c, "setup"):
        raise EconError("You need the Server setup staff permission for this.")
    if act == "import":
        n = await self.count(inter.guild_id)
        if n and not await core.confirm(inter, f"Replace the bot's **{n}** properties with what's in the Google Sheet now? Owners set in "
                                        "the bot but not yet written to the sheet will be lost.", "Import"):
            return
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        res = await self.import_sheet(inter.guild)
        req = self.bot.get_cog("Requests")
        if req:
            await req.sync()
        await inter.followup.send(embed=emb("📥 Imported from Google Sheet",
                                            f"**{res['total']}** properties in **{res['categories']}** categories · **{res['owned']}** owned "
                                            f"({res['linked']} matched to a Discord member). #property-list is updated.", GREEN), ephemeral=True)
        await log(self.bot, inter.guild_id, emb("📥 Property list imported", f"{res['total']} properties by {inter.user.mention}", GOLD))
    elif act == "sheet":
        conf = self.conf
        script = APPS_SCRIPT.replace("%SECRET%", conf["secret"]).replace("%GID%", str(int(conf.get("gid", 0))))
        status = "✅ **Connected**" if conf.get("webhook") else "⚪ **Not connected**"
        pending = (await q1("SELECT COUNT(*) n FROM properties WHERE guild_id=%s AND sheet_synced=0", inter.guild_id))["n"]
        e = emb("🔗 Google Sheet sync", f"{status}" + (f" · {pending} change(s) waiting to be written" if pending else "") +
                f"\nSheet: https://docs.google.com/spreadsheets/d/{conf['sheet_id']}\n\n"
                "**One-time setup (2 minutes):**\n"
                "1. Open the sheet → **Extensions › Apps Script**.\n"
                "2. Delete what's there and paste the attached **sheet-sync.gs** → 💾 Save.\n"
                "3. **Deploy › New deployment** → type **Web app** → Execute as **Me**, Who has access **Anyone** → Deploy → allow access.\n"
                "4. Copy the **Web app URL** and press **🔗 Set web app URL** below.\n\n"
                "-# The script only accepts requests carrying this server's secret key, and only writes the owner / Discord / price "
                "columns of the matching property row. Keep the file private.", BLUE)
        f = discord.File(io.BytesIO(script.encode()), filename="sheet-sync.gs")
        await inter.response.send_message(embed=e, file=f, view=SheetMenu(self), ephemeral=True)


async def _edit(self, inter, ident, owner_raw, civ, price_raw):
    gid = inter.guild_id
    c = await cfg(gid)
    if ident.startswith("#") and ident[1:].isdigit():
        rows = await q("SELECT * FROM properties WHERE guild_id=%s AND id=%s", gid, int(ident[1:]))
    else:
        rows = await q("SELECT * FROM properties WHERE guild_id=%s AND name=%s", gid, ident)
        if not rows:
            rows = await q("SELECT * FROM properties WHERE guild_id=%s AND name LIKE %s LIMIT 6", gid, f"%{ident}%")
    if not rows:
        raise EconError(f"No property matching **{ident}**.")
    if len(rows) > 1:
        raise EconError("More than one match — use the #id:\n" + "\n".join(f"`#{p['id']}` {p['name']} ({p['category']})" for p in rows[:6]))
    p = rows[0]
    price = None
    if price_raw:
        price = parse_price(price_raw)
        if price is None:
            raise EconError("Price must be a number.")
    if owner_raw:
        m = re.search(r"\d{15,21}", owner_raw)
        uid = int(m.group()) if m else self.match_member(inter.guild, owner_raw)
        member = inter.guild.get_member(uid) if uid else None
        if not member:
            raise EconError(f"Couldn't find a server member **{owner_raw}**.")
        await self.set_owner(p["id"], member.id, civ or p["owner_name"] or member.display_name, member.name, price)
        what = f"Owner → {member.mention}" + (f" ({civ})" if civ else "")
    elif civ:
        await self.set_owner(p["id"], None, civ, None, price)
        what = f"Owner → **{civ}** (no Discord account linked)"
    else:
        await self.set_owner(p["id"], None, None, None, price)
        what = "Marked **available** (owner cleared)"
    ok, msg = await self.push(p["id"])
    req = self.bot.get_cog("Requests")
    if req:
        await req.sync()
    txt = f"{what}" + (f"\nPrice → {money(c, price)}" if price is not None else "") + \
        ("\n📄 Google Sheet updated." if ok else f"\n📄 Sheet not updated: {msg}" + (" (will retry)." if self.conf.get("webhook") else "."))
    await reply(inter, embed=emb(f"🏘️ {p['name']}", txt, GREEN), ephemeral=True)
    await log(self.bot, gid, emb("🏘️ Property edited", f"**{p['name']}** — {what} · by {inter.user.mention}", GOLD))


# ═════════════════════ ownership audit ═════════════════════
OWNED_SQL = "(owner_id IS NOT NULL OR COALESCE(TRIM(owner_name),'')<>'' OR COALESCE(TRIM(owner_discord),'')<>'')"


async def audit_queue(cog, gid):
    """Owned properties not yet checked in the current audit round, grouped person by person."""
    rnd = int(cog.conf.get("audit_round_no") or 1)
    rows = list(await q(f"SELECT * FROM properties WHERE guild_id=%s AND {OWNED_SQL} AND audited_round < %s", gid, rnd))
    key = lambda p: (str(p["owner_id"] or ""), (p["owner_name"] or p["owner_discord"] or "").lower())
    rows.sort(key=lambda p: (key(p), CAT_ORDER.get(p["category"], 999), p["sort"]))
    return rows


async def audit_card(cog, gid, c):
    queue = await audit_queue(cog, gid)
    total = (await q1(f"SELECT COUNT(*) n FROM properties WHERE guild_id=%s AND {OWNED_SQL}", gid))["n"]
    if not queue:
        stats = cog.conf.get("audit_stats", {})
        return emb("🔍 Audit complete 🎉", f"Every owned property has been checked this round.\n✅ {stats.get('kept', 0)} still owned · "
                   f"❌ {stats.get('cleared', 0)} cleared · ⏭️ {stats.get('skipped', 0)} skipped\nPress **Start a new round** to go again.",
                   GREEN), None
    p = queue[0]
    same = [x for x in queue if (x["owner_id"], x["owner_name"]) == (p["owner_id"], p["owner_name"])]
    who = " / ".join(x for x in (p["owner_name"], f"<@{p['owner_id']}>" if p["owner_id"] else None,
                                 f"`{p['owner_discord']}`" if p["owner_discord"] and not p["owner_id"] else None) if x)
    e = emb(f"🔍 Does {p['owner_name'] or p['owner_discord'] or 'this owner'} still own this?", None, BLUE)
    e.add_field(name="🏠 Property", value=f"**{p['name']}**", inline=False)
    e.add_field(name="Category", value=f"{p['grp'] or ''} › {p['category']}")
    e.add_field(name="Owner", value=who or "—")
    e.add_field(name="Price", value=money(c, p["price"]) if p["price"] else "—")
    if p["audited_at"]:
        e.add_field(name="Last checked", value=f"<t:{int(p['audited_at'].replace(tzinfo=dt.timezone.utc).timestamp())}:R>", inline=False)
    e.set_footer(text=f"{total - len(queue) + 1} of {total} · this person has {len(same)} left to check")
    return e, p


class AuditView(discord.ui.View):
    def __init__(self, cog, prop):
        super().__init__(timeout=900)
        self.cog, self.prop = cog, prop
        if prop is None:
            for item in list(self.children):
                if getattr(item, "custom_id", None) != "aud:new":
                    self.remove_item(item)
        else:
            self.remove_item(self.new_round)

    async def interaction_check(self, inter):
        if not is_admin(inter.user, await cfg(inter.guild_id), "economy"):
            await inter.response.send_message("Staff only.", ephemeral=True)
            return False
        return True

    async def _next(self, inter, note=None):
        c = await cfg(inter.guild_id)
        e, p = await audit_card(self.cog, inter.guild_id, c)
        if note:
            e.description = (note + ("\n\n" + e.description if e.description else ""))[:4000]
        await inter.response.edit_message(embed=e, view=AuditView(self.cog, p))

    async def _mark(self, inter, outcome):
        p = await self.cog.get(self.prop["id"])
        if not p:
            return await self._next(inter, "⚠️ That property was deleted — skipped.")
        await ex("UPDATE properties SET audited_at=UTC_TIMESTAMP(), audited_by=%s, audited_round=%s WHERE id=%s",
                 inter.user.id, int(self.cog.conf.get("audit_round_no") or 1), p["id"])
        stats = self.cog.conf.setdefault("audit_stats", {})
        stats[outcome] = stats.get(outcome, 0) + 1
        self.cog.bot.state.save()
        return p

    @discord.ui.button(label="Yes, still owns it", emoji="✅", style=discord.ButtonStyle.success, custom_id="aud:yes")
    async def yes(self, inter, b):
        p = await self._mark(inter, "kept")
        if p:
            await self._next(inter, f"✅ **{p['name']}** — kept.")

    @discord.ui.button(label="No, doesn't own it", emoji="❌", style=discord.ButtonStyle.danger, custom_id="aud:no")
    async def no(self, inter, b):
        p = await self._mark(inter, "cleared")
        if not p:
            return
        gid = inter.guild_id
        removed = ""
        if p["owner_id"]:
            n, _ = await ex("UPDATE user_items SET qty=qty-1 WHERE guild_id=%s AND user_id=%s AND name=%s AND qty>0", gid, p["owner_id"], p["name"])
            await ex("DELETE FROM user_items WHERE guild_id=%s AND user_id=%s AND name=%s AND qty<=0", gid, p["owner_id"], p["name"])
            removed = " Removed from their inventory." if n else ""
        await self.cog.set_owner(p["id"], None, None, None)
        ok, msg = await self.cog.push(p["id"])
        sheet = " Sheet updated." if ok else (" Sheet will update once sync is connected." if not self.cog.conf.get("webhook") else " Sheet will retry.")
        await log(self.cog.bot, gid, emb("🔍 Property owner cleared (audit)",
                                         f"**{p['name']}** — was {p['owner_name'] or p['owner_discord'] or '?'}"
                                         + (f" (<@{p['owner_id']}>)" if p["owner_id"] else "") + f" · by {inter.user.mention}", GOLD))
        req = self.cog.bot.get_cog("Requests")
        if req:
            asyncio.create_task(req.sync())
        await self._next(inter, f"❌ **{p['name']}** — now available.{removed}{sheet}")

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="aud:skip")
    async def skip(self, inter, b):
        p = await self._mark(inter, "skipped")
        if p:
            await self._next(inter, f"⏭️ **{p['name']}** — skipped.")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.secondary, custom_id="aud:stop")
    async def stop_(self, inter, b):
        left = len(await audit_queue(self.cog, inter.guild_id))
        await inter.response.edit_message(embed=emb("⏹️ Audit paused", f"{left} property(ies) left this round. Press **🔍 Audit owners** "
                                                    "any time to carry on where you (or anyone else) left off.", GOLD), view=None)

    @discord.ui.button(label="Start a new round", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="aud:new")
    async def new_round(self, inter, b):
        self.cog.conf["audit_round_no"] = int(self.cog.conf.get("audit_round_no") or 1) + 1
        self.cog.conf["audit_stats"] = {}
        self.cog.bot.state.save()
        await self._next(inter, "🔄 New audit round started.")


async def audit_start(cog, inter):
    if not cog.conf.get("audit_round_no"):
        cog.conf["audit_round_no"] = 1
        cog.conf["audit_stats"] = {}
        cog.bot.state.save()
    c = await cfg(inter.guild_id)
    e, p = await audit_card(cog, inter.guild_id, c)
    await inter.response.send_message(embed=e, view=AuditView(cog, p), ephemeral=True)


Properties.admin = _admin
Properties.edit = _edit
