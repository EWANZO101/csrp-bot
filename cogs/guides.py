"""#docs: an interactive how-to guide. Pick a topic from a dropdown, get a short step-by-step guide (only you see it)."""
import logging

import discord
from discord.ext import commands, tasks

from core import cfg

logger = logging.getLogger("economy.guides")

# key: (emoji, title, one-line summary, [steps], tip or None)
PLAYER = {
    "start": ("👋", "Getting started", "Your account, starting money and where things are", [
        "You already have an account — every member gets one with a **starting balance in the bank**.",
        "Type **`/balance`** to see your 💵 wallet and 🏦 bank.",
        "Type **`/bank`** for a menu with buttons for everything money-related.",
        "Browse the **🎮 CSRP Player Hub** channels — each one has buttons, so you rarely need to type commands.",
        "Stuck? Open a ticket in **🎫・support**."], "Money in your **wallet** can be spent anywhere; money in the **bank** is safer."),
    "money": ("💵", "Bank, wallet & paying people", "Deposit, withdraw, pay someone, see history", [
        "**Deposit** wallet → bank: `/deposit amount:500` (or `all`).",
        "**Withdraw** bank → wallet: `/withdraw amount:500`.",
        "**Pay someone**: `/pay user:@name amount:250` — you can add a note.",
        "**See what happened**: `/history` shows your recent transactions.",
        "**Richest players**: `/leaderboard`."], "Amounts like `5k`, `2.5m` and `all` work everywhere."),
    "chars": ("🎭", "Characters", "Play several characters, each with their own money & items", [
        "Go to **🎭・characters** and press **➕ Create** (or `/character create name:Tony`).",
        "Choose whether to **switch to them now**. New characters start with the starting balance and an empty inventory.",
        "Press **🔁 Switch** any time to change who you're playing. The other character's money and items wait safely.",
        "**📋 My characters** shows each one's wallet, bank and items.",
        "**✏️ Rename** or **🗑️ Delete** extra characters (delete asks first; the main character can't be deleted)."],
        "Money imported from the old bot always goes to your **main** character."),
    "shop": ("🛍️", "Shopping", "Find and buy things from businesses", [
        "See everything for sale: **`/shop all`**.",
        "Look inside one shop: **`/shop browse store:`** and start typing the business name — pick from the list.",
        "Buy from the menu that appears, or type **`/shop buy store:… item:… quantity:1`**.",
        "The money comes out of your **bank** and the item goes into your inventory."], "If a shop says *out of stock*, ask the owner to restock."),
    "inv": ("🎒", "Inventory, giving & using items", "See your items, give them away, use them", [
        "See your items: **`/inventory`** (or `/inventory user:@name` for someone else).",
        "Give an item: **`/give user:@name item:`** — start typing and pick the item, then the quantity.",
        "Use an item: **`/use item:`** — some items give you a role for a while, some get used up."], None),
    "biz": ("🏢", "Owning a business", "Get a business, manage money, staff and stock", [
        "Ask for a business in **🏢・business-requests** (press 📝) — staff approve it and it's yours.",
        "Put money in: `/business deposit` · take it out: `/business withdraw` · pay someone: `/business send`.",
        "Add things to sell: `/business item add` (price, stock, description). Edit with `/business item edit`.",
        "Give staff access: `/business role add` (head or member) and pay them with `/payroll add`.",
        "See it all: `/business info` · hand it over: `/business transfer_ownership`."],
        "Shop sales go straight into your business account."),
    "job": ("💼", "Getting paid (payroll)", "How wages work", [
        "Your boss adds you with `/payroll add` and sets your pay.",
        "Pay arrives in your **bank** automatically on payday (or when they run `/payroll run`).",
        "You get a DM each payday. Check with `/history`."], "Not paid? The business may be out of money — tell your boss."),
    "fines": ("🧾", "Citations & fines", "See and pay fines", [
        "See your fines: **`/citation mine`**.",
        "Pay one: **`/citation pay id:12`** (from your bank), or press **💳 Pay now** in the DM you got.",
        "Pay before the due date — overdue fines get a **late fee**."], "Medical bills from the hospital show here too."),
    "hospital": ("🏥", "Hospital (patient)", "What happens when you're admitted and how to pay", [
        "When medics admit you, you get a DM and the clock starts — you're billed **per second** while admitted.",
        "Every 30 seconds the cost so far is taken from your **bank**. If you run out, the rest becomes debt.",
        "Check your bill any time: **💳 My bill** in **🏥・hospital** (or `/hospital bill`).",
        "When you're discharged, anything unpaid becomes a **medical bill** — pay it with the 💳 button in the DM "
        "or `/citation pay`."], "Keep some money in the bank — it's used first."),
    "utilities": ("💡", "Utilities (power, water, phone…)", "Set up payment, sign up, pay bills", [
        "In **💡・utilities** press **💳 Payment menu** first — pick **bank autopay**, **wallet autopay** or **manual**, "
        "plus a backup. Press **Save**.",
        "Press **➕ Sign up**, choose a company, then a plan, then **Sign up**.",
        "Bills arrive every cycle. With autopay they're paid automatically (and cheaper!).",
        "**📄 My bills** shows what you owe — pay one or **Pay all**.",
        "Late? A late fee is added, and after a while the service is **shut off**. Pay up, then press **🔌 Reconnect** "
        "in **🔌 My services**."], "Autopay retries automatically as soon as you have money."),
    "property": ("🏠", "Requesting a property or business", "Ask staff for a house or a business", [
        "Go to **🏠・property-requests** (or **🏢・business-requests**).",
        "Press **📝** and fill in what you want.",
        "Staff approve or deny it — you get a DM either way. There may be a price, taken from your bank.",
        "Approved properties appear in your inventory; approved businesses are yours to run."], None),
    "help": ("🎫", "Support, applications & documents", "Get help, apply for jobs, read rules", [
        "**Need help?** Open a ticket in **🎫・support**.",
        "**Apply** for a job or role: **📝・applications** or `/apply`.",
        "**Rules, SOPs, penal codes**: **📚・documents** or `/docs`."], None),
    "events": ("🗓️", "Events & who's online", "Server events and bot activity", [
        "**🗓️・schedule** shows upcoming events with countdowns in *your* timezone, plus reminders.",
        "**📈・bot-activity** shows who's using the bot right now and the most active this week.",
        "Type `/active` to see it privately."], None),
}

STAFF = {
    "access": ("🔑", "Giving someone staff access", "Staff ranks and who can see staff channels", [
        "Open the web panel **https://bot.snailyapp.com** and log in with Discord.",
        "Go to **Staff** → pick a rank (Admin, Moderator, Support…) → add their **role** or **user**.",
        "Each rank has permissions: *Citizens & money*, *Businesses*, *Citations*, *Server setup*…",
        "In Discord run **`/staff_panel`** and **`/player_accounts`** so the staff channels let them in."],
        "Bot owners always have every permission."),
    "panel": ("🛠️", "Using the staff panel", "Everything in #🛠️・staff-panel", [
        "Go to **🛠️・staff-panel** and press a button — a form pops up.",
        "**Player** boxes: start typing a name and pick from the list.",
        "**Business / item** boxes: type part of the name — the closest match is used (the reply tells you which).",
        "Replies are only visible to you; money and item changes are posted to **📒・bot-logs**."],
        "The 🏥 Hospital admin card lives in the same channel."),
    "money": ("💰", "Money: print, remove, freeze, undo", "Fix balances and mistakes", [
        "**Give money**: 💰 Print money in the staff panel, or `/admin print amount:5000 user:@name`.",
        "**Take money**: 🔥 Remove money, or `/admin remove`.",
        "**Freeze** an account (blocks spending): 🧊 in the panel or `/admin freeze`.",
        "**Undo** a transaction: find its # with `/admin audit` (or 🧾 Transactions), then `/admin undo id:123`."],
        "For a business, leave the player empty and type the business name."),
    "items": ("📦", "Moving items between players", "Fix inventories or transfer everything", [
        "**📦 Move items** in the staff panel (or `/admin move_items`).",
        "Pick **from** and **to** players. Type an item, or leave it **empty to move everything**.",
        "Optional quantity — empty means all of it.",
        "Also on the web panel: Citizens → click a player → *Move items*."], None),
    "orgs": ("🏢", "Businesses, departments & shops", "Create them and set up what they sell", [
        "Create a department: `/department create` · a business: `/business create` (or approve a business request).",
        "Add shop items: **🛒 Add / edit shop item** in the staff panel, or `/business item add`.",
        "Give an item from stock to a player for free: **🎁 Give item from stock**.",
        "Change an owner: **👑 Set business owner**. See details: **🏢 Business info**."], None),
    "payroll": ("💼", "Payroll", "Paying staff of businesses & departments", [
        "Add someone: `/payroll add org:… user:@name amount:500`.",
        "Pay now: `/payroll run`. Automatic payroll runs every *N* hours — change with "
        "`/admin config key:payroll_hours value:168` (0 = manual).",
        "Want a fixed day/time instead? Use `/schedule payroll when:fri 18:00 repeat:Weekly` and set payroll_hours to 0."], None),
    "hospital": ("🏥", "Running the hospital", "Billing settings, admitting, bills", [
        "In **🛠️・staff-panel → 🏥 Hospital admin** press **⚙️ Billing settings**: set the **hospital department** "
        "(who gets paid), cost per second, admission fee, cap and days to pay.",
        "Medics use **🏥 Admit patient** / **🩺 Discharge** in **🏥・hospital** (or `/hospital admit|discharge`).",
        "Medics = members of the hospital department, or staff with Citations / Citizens & money.",
        "See unpaid bills (**🧾**), cancel one (**💸 Waive a bill**) or view **📊 Stats** in the admin card."],
        "If no department is set, medical payments just disappear."),
    "utilities": ("💡", "Setting up utilities", "Companies, plans, rules, opening it up", [
        "Open **💡・utilities-admin** and press **🌱 Starter pack** — 7 companies with plans are created.",
        "Press **🏢 Company** for each one and set the **business/department that gets paid**.",
        "Adjust **📦 Plans** (price, cycle, usage swing, setup fee) and **⚙️ Billing rules** (late fee, days to pay, "
        "shut-off, reconnect fee, autopay discount).",
        "Test it yourself in **💡・utilities** (staff can use it while locked).",
        "When ready press **🔓 Open to players**."], "📊 Overview shows customers, revenue and who owes."),
    "schedule": ("🗓️", "Scheduling things", "Buttons in #🗓️・schedule, or slash commands", [
        "Open **🗓️・schedule**. Staff see buttons: **📅 Add event**, **📣 Announcement**, **💸 Payment**, **💼 Payroll**, "
        "**🗂️ Manage** and **🌍 Timezone**.",
        "Press **🌍 Timezone** once and type yours, e.g. `America/New_York`.",
        "Press a button, fill in the form and choose how often it repeats. **When** can be `in 2h`, `20:00`, `tomorrow 8pm` or `fri 19:30`.",
        "Events show on the schedule card with a countdown, a reminder before they start and a *starting now* post.",
        "**🗂️ Manage** lets you pause, resume, run now or delete anything that's scheduled.",
        "Prefer commands? `/schedule announce`, `/schedule money`, `/schedule event`, `/schedule payroll` and `/schedule list` do the same."],
        "Times: `in 2h`, `20:00`, `tomorrow 8pm`, `fri 19:30`, `2026-11-20 18:00`."),
    "requests": ("📝", "Business & property requests", "Approve, deny and open the channels", [
        "Requests appear in **🏢・business-requests-admin** / **🏠・property-requests-admin** with ✅ Approve / ❌ Deny.",
        "When approving you can set a **price** (taken from the player's bank) and a **note**.",
        "Approved business → created and owned by the player. Approved property → added to their inventory.",
        "The player channels start private — open them from the admin card when you're ready."], None),
    "import": ("📥", "Importing from the old bot", "Bring balances, items and shops across", [
        "In **#banks-inventory** run the old bot's **`/balance user:@player`** — it's imported within seconds.",
        "Inventories: run its inventory command there and **click through every page** (Next, Next…).",
        "Shops: run **`business_inventory`** for each business.",
        "**Nothing doubles** — the first card *sets* the value, later cards only apply changes.",
        "Images posted there are saved; ask Claude to *read new data* and they're imported the same safe way."],
        "Every import is posted in 📒・bot-logs."),
    "accounts": ("📊", "Player accounts directory", "Everyone's money & items in one place", [
        "Open **📊・player-accounts** — one card, richest first, 20 per page.",
        "Use **◀ Prev / Next ▶** to page through.",
        "Type a name in **🔎** to see that player's full account (all items, businesses) — only you see it.",
        "Updates every minute. `/player_accounts` refreshes it now."], None),
    "chars": ("🎭", "Characters (staff)", "Limits and viewing players' characters", [
        "Change the limit: `/admin config key:max_characters value:3`.",
        "See anyone's characters: `/character list user:@name`.",
        "Each character has its own wallet, bank and inventory; businesses, payroll and citations stay with the Discord user."], None),
    "fines": ("🚔", "Issuing citations", "Fines for department members", [
        "Department members: `/citation issue org:LSPD user:@name amount:500 reason:…`.",
        "Department heads can `/citation edit` and `/citation void`.",
        "Players pay with `/citation pay`; overdue ones get the late fee (`/admin config key:late_fee`)."], None),
    "panels": ("🧩", "Tickets, applications & documents", "Posting the panels", [
        "Tickets: `/ticket panel` in the channel you want.",
        "Applications: `/apply_panel` — build the forms on the web panel.",
        "Documents: `/docs_panel` — add documents on the web panel.",
        "Settings for all of these: web panel → **Settings**."], None),
}


def guide_view(entry, audience):
    emoji, title, summary, steps, tip = entry
    body = "\n".join(f"**{i}.** {s}" for i, s in enumerate(steps, 1))
    parts = [discord.ui.TextDisplay(f"## {emoji} {title}\n-# {audience} guide · {summary}"), discord.ui.Separator(),
             discord.ui.TextDisplay(body)]
    if tip:
        parts += [discord.ui.Separator(), discord.ui.TextDisplay(f"💡 **Tip:** {tip}")]
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(discord.ui.Container(*parts, accent_colour=discord.Colour.blurple() if audience == "Player" else discord.Colour.dark_red()))
    return v


class DocsCard(discord.ui.LayoutView):
    def __init__(self, header=None):
        super().__init__(timeout=None)
        p = discord.ui.Select(custom_id="docs:player", placeholder="🎮 Player guides — what do you want to know?", options=[
            discord.SelectOption(label=t, value=k, emoji=e, description=s[:100]) for k, (e, t, s, _, _) in PLAYER.items()])
        st = discord.ui.Select(custom_id="docs:staff", placeholder="🛠️ Staff guides — setup & admin", options=[
            discord.SelectOption(label=t, value=k, emoji=e, description=s[:100]) for k, (e, t, s, _, _) in STAFF.items()])
        p.callback = st.callback = self.picked
        menu_p = "\n".join(f"{e} {t}" for e, t, *_ in PLAYER.values())
        menu_s = "\n".join(f"{e} {t}" for e, t, *_ in STAFF.values())
        self.add_item(discord.ui.Container(
            *(header or [discord.ui.TextDisplay("## 📖 How to do everything")]), discord.ui.Separator(),
            discord.ui.TextDisplay("### 🎮 For players"), discord.ui.TextDisplay(f"-# {menu_p.replace(chr(10), ' · ')}"),
            discord.ui.ActionRow(p), discord.ui.Separator(),
            discord.ui.TextDisplay("### 🛠️ For staff"), discord.ui.TextDisplay(f"-# {menu_s.replace(chr(10), ' · ')}"),
            discord.ui.ActionRow(st), accent_colour=discord.Colour.teal()))

    async def picked(self, inter: discord.Interaction):
        cid, key = inter.data["custom_id"], inter.data["values"][0]
        table, aud = (PLAYER, "Player") if cid == "docs:player" else (STAFF, "Staff")
        entry = table.get(key)
        if not entry:
            return await inter.response.send_message("That guide no longer exists.", ephemeral=True)
        await inter.response.send_message(view=guide_view(entry, aud), ephemeral=True)


class Guides(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(DocsCard())
        self.refresh.start()

    def cog_unload(self):
        self.refresh.cancel()

    @property
    def conf(self):
        return self.bot.state.settings.setdefault("guides", {})

    async def sync(self):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            head = [discord.ui.TextDisplay(
                "## 📖 How to do everything\nPick a topic from a menu below — you'll get a short **step-by-step** guide "
                "that only you can see. Pick as many as you like.")]
            view = DocsCard(head)
            ch = g.get_channel(int(self.conf.get("channel_id") or 0))
            if not ch:
                hub = g.get_channel(1571328922388271264) or getattr(g.get_channel((await cfg(gid)).get("log_channel") or 0), "category", None)
                ow = {g.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False),
                      g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
                ch = await g.create_text_channel("📖・docs", overwrites=ow, category=hub if isinstance(hub, discord.CategoryChannel) else None,
                                                 position=0, topic="How to do everything — pick a topic for a step-by-step guide")
                self.conf.update(channel_id=ch.id, message_id=None)
                logger.info("created #%s (%s)", ch.name, ch.id)
            ok = False
            if self.conf.get("message_id"):
                try:
                    await ch.get_partial_message(int(self.conf["message_id"])).edit(view=view)
                    ok = True
                except discord.NotFound:
                    pass
            if not ok:
                msg = await ch.send(view=view)
                self.conf["message_id"] = msg.id
            self.bot.state.save()

    @tasks.loop(hours=6)
    async def refresh(self):
        try:
            await self.sync()
        except Exception:
            logger.exception("docs card refresh failed")

    @refresh.before_loop
    async def _w(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Guides(bot))
