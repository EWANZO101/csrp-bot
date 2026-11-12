"""Entry point: web panel always runs; the Discord bot starts once it has been set up through the panel."""
import os, json, asyncio, secrets, logging
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

import discord
import core

log = logging.getLogger("economy")
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE, "data"))
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
CODE_FILE = os.path.join(DATA_DIR, "setup-code.txt")


class AppState:
    """Holds the bot-connection settings (set via web UI) and the running bot."""

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.settings = {}
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE) as f:
                self.settings = json.load(f)
        if not self.settings.get("web_secret"):
            self.settings["web_secret"] = secrets.token_hex(32)
            self.save()
        env_url = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
        if env_url and self.settings.get("public_url") != env_url:
            self.settings["public_url"] = env_url
            self.save()
        if not os.path.exists(CODE_FILE):
            with open(CODE_FILE, "w") as f:
                f.write(secrets.token_hex(4).upper() + "\n")
            os.chmod(CODE_FILE, 0o600)
        self.bot = None
        self.task = None
        self.status = "not configured"
        self.error = None
        self.warning = None

    @property
    def setup_code(self) -> str:
        with open(CODE_FILE) as f:
            return f.read().strip()

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.get("token") and s.get("guild_id") and s.get("client_id"))

    @property
    def guild_id(self) -> int | None:
        g = self.settings.get("guild_id")
        return int(g) if g else None

    def save(self):
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.settings, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_FILE)

    async def stop_bot(self):
        if self.bot and not self.bot.is_closed():
            await self.bot.close()
        if self.task:
            try:
                await asyncio.wait_for(self.task, 10)
            except Exception:
                pass
        self.bot, self.task = None, None

    async def start_bot(self):
        await self.stop_bot()
        self.error = self.warning = None
        if not self.configured:
            self.status = "not configured"
            return
        core.BOT_OWNERS.clear()
        core.BOT_OWNERS.update(int(x) for x in self.settings.get("owner_ids", []) if str(x).isdigit())
        from bot import EconomyBot
        self.status = "starting"

        async def run(members: bool):
            bot = EconomyBot(self, members=members)
            self.bot = bot
            try:
                await bot.start(self.settings["token"])
            except discord.PrivilegedIntentsRequired:
                await bot.close()
                if members:
                    self.warning = ("Server Members Intent is off — names in the panel may show as IDs. Turn it on in "
                                    "Discord Developer Portal → Bot → Privileged Gateway Intents, then restart the bot.")
                    return await run(False)
                self.status, self.error = "error", "Discord refused the gateway intents."
            except discord.LoginFailure:
                self.status, self.error = "error", "Discord rejected the bot token."
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("bot crashed")
                self.status, self.error = "error", f"{type(e).__name__}: {e}"
            finally:
                if not bot.is_closed():
                    await bot.close()
                if self.status not in ("error",):
                    self.status = "stopped"

        self.task = asyncio.create_task(run(True))


async def amain():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for attempt in range(30):  # DB container may still be starting
        try:
            await core.init_db()
            break
        except Exception as e:
            log.warning("database not ready (%s), retrying…", e)
            await asyncio.sleep(2)
    else:
        raise SystemExit("could not connect to the database")
    state = AppState()
    import hub_client
    state.hub = hub_client.Hub(state, DATA_DIR)
    asyncio.create_task(state.hub.run())
    from web import Panel
    panel = Panel(state)
    await panel.start()
    if state.configured:
        await state.start_bot()
    else:
        log.warning("=" * 60)
        log.warning("  Not set up yet — open the web panel in your browser and paste your bot token.")
        log.warning("=" * 60)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
