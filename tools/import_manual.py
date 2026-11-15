"""Import data read off an image (or typed by hand) through the importer's normal snapshot logic — so nothing doubles.

Run inside the bot container:
    python tools/import_manual.py card.json [--dry-run]

card.json — one card, written to look like the old bot's embed:
{
  "msg_id": 1571330000000000000,          # the Discord message the image came from (orders snapshots)
  "image": "1571330000000000000_0_x.png", # optional: marks that import_images row as imported
  "title": "💰 Account Balance",          # or "🎒 Inventory for <name>", "📦 Inventory — <business>", "📋 Business List"
  "description": "Showing balances for <@123456789012345678>",
  "fields": [["💵 Wallet", "$7,220.00"], ["🏦 Bank", "$15,572,034.00"]],
  "footer": "Page 1 / 6"
}
For inventories put the owner's mention (<@id>) as the first description line, then one item per line.
"""
import asyncio, json, os, sys, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import discord
import core
from cogs.importer import Importer


async def main(path, dry):
    card = json.load(open(path))
    await core.init_db()
    settings = json.load(open(os.path.join(os.getenv("DATA_DIR", "/app/data"), "settings.json")))
    gid = int(settings["guild_id"])
    e = discord.Embed(title=card.get("title"), description=card.get("description"))
    for n, v in card.get("fields", []):
        e.add_field(name=n, value=v, inline=False)
    if card.get("footer"):
        e.set_footer(text=card["footer"])
    bot = types.SimpleNamespace(user=types.SimpleNamespace(id=int(settings["client_id"])), state=types.SimpleNamespace(settings=settings))
    guild = types.SimpleNamespace(id=gid, members=[])
    msg = types.SimpleNamespace(id=int(card["msg_id"]), guild=guild, channel=types.SimpleNamespace(id=0),
                                interaction_metadata=None, interaction=None, embeds=[e])
    imp = Importer.__new__(Importer)
    imp.bot = bot
    if dry:
        print("DRY RUN — parsed card:", e.to_dict())
        return
    async with core.IMPORT_LOCK if hasattr(core, "IMPORT_LOCK") else asyncio.Lock():
        kind, changes = await imp.apply_embed(gid, msg, e)
    print("kind:", kind)
    print("changes:" if changes else "no changes (already up to date)")
    for c in changes:
        print("  ", c)
    if card.get("image"):
        await core.ex("UPDATE import_images SET status='imported', note=%s WHERE path LIKE %s",
                      ("; ".join(changes) or "no changes")[:500], "%" + card["image"])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], "--dry-run" in sys.argv))
