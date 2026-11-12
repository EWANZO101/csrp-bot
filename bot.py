import os, logging

import discord
from discord import app_commands
from discord.ext import commands

import core
from core import EconError, emb, RED

log = logging.getLogger("economy")

EXTENSIONS = ["cogs.bank", "cogs.orgs", "cogs.items", "cogs.payroll", "cogs.citations", "cogs.admin"]
OPTIONAL = {"tickets": "cogs.tickets", "documents": "cogs.documents", "applications": "cogs.applications", "cad": "cogs.cad"}


class EconomyBot(commands.Bot):
    def __init__(self, state, members=True):
        intents = discord.Intents.default()
        intents.members = members
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.state = state

    @property
    def features(self) -> dict:
        return {**core.DEFAULT_FEATURES, **(self.state.settings.get("features") or {})}

    @property
    def guild_ids(self):
        return [self.state.guild_id] if self.state.guild_id else []

    async def setup_hook(self):
        feats = self.features
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        for feat, ext in OPTIONAL.items():
            if feats.get(feat):
                await self.load_extension(ext)
        for feat, names in core.FEATURE_COMMANDS.items():
            if not feats.get(feat):
                for n in names:
                    self.tree.remove_command(n)
        self.tree.on_error = self.on_app_error
        for gid in self.guild_ids:
            g = discord.Object(id=gid)
            self.tree.copy_global_to(guild=g)
            try:
                synced = await self.tree.sync(guild=g)
                log.info("synced %d commands to guild %s", len(synced), gid)
            except discord.Forbidden:
                self.state.warning = "The bot isn't in your server yet — use the invite link in Settings, then restart the bot."
        self.tree.clear_commands(guild=None)
        await self.tree.sync()  # keep global scope empty; commands live on the configured server

    async def on_ready(self):
        self.state.status = "online"
        log.info("logged in as %s (%s) in %d guilds", self.user, self.user.id, len(self.guilds))
        if self.guild_ids and not self.get_guild(self.guild_ids[0]):
            self.state.warning = "The bot isn't in your server yet — use the invite link in Settings, then restart the bot."
        elif self.state.warning and "isn't in your server" in self.state.warning:
            self.state.warning = None
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="the economy · /help"))

    async def on_guild_join(self, guild):
        if self.guild_ids and guild.id not in self.guild_ids:
            log.warning("leaving unauthorised guild %s", guild.id)
            await guild.leave()
        elif guild.id in self.guild_ids:
            await self.tree.sync(guild=guild)
            self.state.warning = None
            log.info("joined configured guild %s — commands synced", guild.id)

    async def on_app_error(self, inter: discord.Interaction, error: app_commands.AppCommandError):
        err = getattr(error, "original", error)
        if isinstance(err, EconError):
            msg = str(err)
        elif isinstance(error, app_commands.CheckFailure):
            msg = "You can't use this command."
        else:
            log.error("command error in /%s", inter.command.qualified_name if inter.command else "?",
                      exc_info=(type(err), err, err.__traceback__))
            msg = "Something went wrong — staff can check the logs."
        e = emb("❌ " + msg, None, RED) if len(msg) <= 240 else emb("❌ Error", msg, RED)
        try:
            await core.reply(inter, embed=e, ephemeral=True)
        except discord.HTTPException:
            pass
