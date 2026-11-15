"""#characters: a public, read-only card with a button per /character command.

Buttons call the exact same handlers as the slash commands in cogs/characters.py (create / switch / list / rename /
delete), so behaviour, limits and confirmations are identical.
"""
import logging

import discord
from discord.ext import commands, tasks

import core
from core import EconError, q, cfg, money, emb, reply, RED, BLUE

logger = logging.getLogger("economy.charpanel")


def chars_cog(bot):
    cog = bot.get_cog("Characters")
    if not cog:
        raise EconError("The character system isn't loaded right now.")
    return cog


async def run(inter, fn):
    try:
        await fn()
    except EconError as err:
        await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class CreateModal(discord.ui.Modal, title="🎭 Create a character"):
    name = discord.ui.Label(text="Character name", description="1–32 characters",
                            component=discord.ui.TextInput(max_length=32, placeholder="e.g. Tony Romano"))
    switch = discord.ui.Label(text="Switch to them now?", component=discord.ui.Select(options=[
        discord.SelectOption(label="Yes — play as them straight away", value="1", default=True),
        discord.SelectOption(label="No — keep playing as my current character", value="0")]))

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, inter):
        cog = chars_cog(self.bot)
        await run(inter, lambda: cog.create.callback(cog, inter, name=self.name.component.value,
                                                     switch_now=(self.switch.component.values or ["1"])[0] == "1"))


class RenameModal(discord.ui.Modal, title="✏️ Rename character"):
    new = discord.ui.Label(text="New name", description="1–32 characters", component=discord.ui.TextInput(max_length=32))

    def __init__(self, bot, old):
        super().__init__()
        self.bot, self.old = bot, old

    async def on_submit(self, inter):
        cog = chars_cog(self.bot)
        await run(inter, lambda: cog.rename.callback(cog, inter, name=self.old, new_name=self.new.component.value))


class PickChar(discord.ui.View):
    """Ephemeral dropdown of the clicker's characters, then runs an action."""

    def __init__(self, bot, chars, action, c):
        super().__init__(timeout=180)
        self.bot, self.action = bot, action
        opts = [discord.SelectOption(label=ch["name"][:100], value=ch["name"],
                                     emoji="🟢" if ch["active"] else "⚪",
                                     description=(("active · " if ch["active"] else "")
                                                  + ("main" if ch["is_main"] else "extra character"))[:100]) for ch in chars[:25]]
        s = discord.ui.Select(placeholder={"switch": "Who do you want to play as?", "rename": "Which character?",
                                           "delete": "Which character to delete?"}[action], options=opts)
        s.callback = self.picked
        self.add_item(s)

    async def picked(self, inter):
        name = inter.data["values"][0]
        cog = chars_cog(self.bot)
        if self.action == "switch":
            await run(inter, lambda: cog.switch.callback(cog, inter, name=name))
        elif self.action == "rename":
            await inter.response.send_modal(RenameModal(self.bot, name))
        else:
            await run(inter, lambda: cog.delete.callback(cog, inter, name=name))


class CharCard(discord.ui.LayoutView):
    def __init__(self, bot, header=None):
        super().__init__(timeout=None)
        self.bot = bot
        b = {k: discord.ui.Button(label=l, emoji=e, style=s, custom_id=f"chars:{k}") for k, l, e, s in [
            ("create", "Create", "➕", discord.ButtonStyle.success), ("switch", "Switch", "🔁", discord.ButtonStyle.primary),
            ("list", "My characters", "📋", discord.ButtonStyle.secondary), ("rename", "Rename", "✏️", discord.ButtonStyle.secondary),
            ("delete", "Delete", "🗑️", discord.ButtonStyle.danger)]}
        for x in b.values():
            x.callback = self.on_button
        S, T = discord.ui.Section, discord.ui.TextDisplay
        gap = lambda: discord.ui.Separator(visible=False)
        self.add_item(discord.ui.Container(
            *(header or [T("## 🎭 Characters")]), discord.ui.Separator(), T("### 📋 Commands"),
            S(T("**➕ `/character create <name>`**\nMakes a new character with the server's starting balance and an empty "
                "inventory, and switches to it (you can turn that off with switch_now)."), accessory=b["create"]), gap(),
            S(T("**🔁 `/character switch <name>`**\nPlays as another character. The current one's money and items are kept "
                "until they switch back."), accessory=b["switch"]), gap(),
            S(T("**📋 `/character list`**\nShows the player's characters with each one's wallet, bank and item count. "
                "Staff can view anyone's."), accessory=b["list"]), gap(),
            S(T("**✏️ `/character rename`**\nGive one of your characters a new name."), accessory=b["rename"]), gap(),
            S(T("**🗑️ `/character delete`**\nDelete asks for confirmation first, and the character's money and items are lost. "
                "The main character can't be deleted."), accessory=b["delete"]),
            accent_colour=discord.Colour.purple()))

    async def on_button(self, inter: discord.Interaction):
        k = inter.data["custom_id"].split(":")[1]
        try:
            cog = chars_cog(self.bot)
            if k == "create":
                return await inter.response.send_modal(CreateModal(self.bot))
            if k == "list":
                return await run(inter, lambda: cog.list_.callback(cog, inter, user=None))
            from cogs.characters import ensure_main, characters_of
            await ensure_main(inter.guild_id, inter.user)
            chars = await characters_of(inter.guild_id, inter.user.id)
            if k == "switch":
                chars = [ch for ch in chars if not ch["active"]]
                if not chars:
                    raise EconError("You only have one character — press ➕ Create to make another.")
            if k == "delete":
                chars = [ch for ch in chars if not ch["is_main"] and not ch["active"]]
                if not chars:
                    raise EconError("Nothing to delete — your main character and the one you're playing can't be deleted.")
            c = await cfg(inter.guild_id)
            await inter.response.send_message(view=PickChar(self.bot, chars, k, c), ephemeral=True)
        except EconError as err:
            await reply(inter, embed=emb("❌ " + str(err)[:240], None, RED), ephemeral=True)


class CharPanel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(CharCard(bot))
        self.refresh.start()

    def cog_unload(self):
        self.refresh.cancel()

    @property
    def conf(self):
        return self.bot.state.settings.setdefault("char_panel", {})

    async def sync(self):
        for gid in self.bot.guild_ids:
            g = self.bot.get_guild(gid)
            if not g:
                continue
            c = await cfg(gid)
            st = (await q("SELECT COUNT(*) n, COUNT(DISTINCT user_id) u FROM characters WHERE guild_id=%s", gid))[0]
            head = [discord.ui.TextDisplay(
                f"## 🎭 Characters\nPlay more than one character — each has its **own wallet, bank and inventory**.\n"
                f"Up to **{c.get('max_characters', 3)}** characters each · new characters start with "
                f"**{money(c, c['start_balance'])}** · your main character receives imports from the old bot.\n"
                f"-# {st['n']} characters across {st['u']} players · updated <t:{int(discord.utils.utcnow().timestamp())}:R>")]
            view = CharCard(self.bot, head)
            ch = g.get_channel(int(self.conf.get("channel_id") or 0))
            if not ch:
                logch = g.get_channel(c.get("log_channel") or 0)
                ow = {g.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False),
                      g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
                ch = await g.create_text_channel("characters", overwrites=ow, category=getattr(logch, "category", None),
                                                 topic="Create and switch characters — each has its own money and items")
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

    @tasks.loop(minutes=2)
    async def refresh(self):
        try:
            await self.sync()
        except Exception:
            logger.exception("character panel refresh failed")

    @refresh.before_loop
    async def _w(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(CharPanel(bot))
