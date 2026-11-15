"""Everything the bot knows about one player, as a list of embeds (used by tickets → 📋 Player info).

Sections: profile & user setup, money (active + parked characters), inventory, businesses, properties,
playtime (GTA V sessions, patrols, bot use), bills / citations / hospital, and recent history.
Every section is best-effort: a missing table or bad row skips that bit instead of failing the whole card.
"""
import datetime as dt
import json
import logging

import discord

from core import q, q1, money, BLUE, GREEN, GOLD

logger = logging.getLogger("economy.player_info")
DEPT_LABELS = {"civ": "🧍 Civilian", "sast": "🚔 SAST", "bcso": "⭐ BCSO", "lspd": "🚓 LSPD", "safd": "🚒 SAFD", "dispatch": "📟 Dispatch"}


def _now():
    return dt.datetime.utcnow().replace(microsecond=0)


def _t(d, style="R"):
    return f"<t:{int(d.replace(tzinfo=dt.timezone.utc).timestamp())}:{style}>" if d else "—"


def _hm(sec) -> str:
    sec = max(int(sec or 0), 0)
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _clip(lines, limit=1024, empty="—") -> str:
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit - 20:
            out.append(f"…and {len(lines) - i} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or empty


async def _safe(coro, default=None):
    try:
        return await coro
    except Exception:
        logger.exception("player info query failed")
        return default


async def _played(gid, uid, since):
    t = _now()
    r = await q1("SELECT SUM(TIMESTAMPDIFF(SECOND, GREATEST(started_at, %s), COALESCE(ended_at, %s))) s FROM gta_sessions "
                 "WHERE guild_id=%s AND user_id=%s AND COALESCE(ended_at, %s) > %s", since, t, gid, uid, t, since)
    return int((r or {}).get("s") or 0)


async def build(guild: discord.Guild, uid: int, c: dict) -> list[discord.Embed]:
    gid = guild.id
    member = guild.get_member(uid)
    m = lambda n: money(c, int(n or 0))
    embeds = []

    # ───── 👤 profile ─────
    name = member.display_name if member else str(uid)
    e = discord.Embed(title=f"📋 Player info — {name}", colour=BLUE,
                      description=f"<@{uid}> · `{uid}`" + ("" if member else "\n⚠️ **Not in the server any more**"))
    if member:
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="Discord", value=f"`{member.name}`\nAccount made {_t(member.created_at.replace(tzinfo=None))}")
        e.add_field(name="Joined server", value=_t(member.joined_at.replace(tzinfo=None)) if member.joined_at else "—")
        roles = [r.mention for r in sorted(member.roles[1:], key=lambda r: -r.position)]
        e.add_field(name=f"Roles ({len(roles)})", value=_clip([" ".join(roles[:25])] if roles else [], 1024, "None"), inline=False)
    setup = await _safe(q1("SELECT * FROM form_entries WHERE guild_id=%s AND user_id=%s AND kind='setup' ORDER BY id DESC LIMIT 1", gid, uid))
    if setup:
        d = json.loads(setup["data"])
        depts = ", ".join(DEPT_LABELS.get(x, x) for x in d.get("depts") or []) or d.get("dept_text") or "—"
        e.add_field(name="📋 User setup", value=f"**{d.get('name') or '—'}** · 🎮 `{d.get('gamertag') or '—'}`\n{depts}\n"
                    f"-# {setup['status']} · {_t(setup['created_at'], 'd')}", inline=False)
    else:
        e.add_field(name="📋 User setup", value="Never filled out", inline=False)
    transfers = await _safe(q("SELECT status, data, created_at FROM form_entries WHERE guild_id=%s AND user_id=%s AND kind='transfer' "
                              "ORDER BY id DESC LIMIT 3", gid, uid), [])
    if transfers:
        lines = []
        for r in transfers:
            d = json.loads(r["data"])
            lines.append(f"{DEPT_LABELS.get(d.get('from'), d.get('from_text') or '?')} → {DEPT_LABELS.get(d.get('to'), d.get('to_text') or '?')}"
                         f" · {r['status']} · {_t(r['created_at'], 'd')}")
        e.add_field(name="🔁 Transfers", value=_clip(lines), inline=False)
    roster = await _safe(q("SELECT section, title FROM roster WHERE guild_id=%s AND user_id=%s ORDER BY section, sort", gid, uid), [])
    if roster:
        e.add_field(name="🗂️ Roster", value=_clip([f"**{r['section']}** — {r['title'] or 'member'}" for r in roster]), inline=False)
    embeds.append(e)

    # ───── 💰 money & characters ─────
    acc = await _safe(q1("SELECT * FROM users WHERE guild_id=%s AND user_id=%s", gid, uid))
    chars = await _safe(q("SELECT * FROM characters WHERE guild_id=%s AND user_id=%s ORDER BY is_main DESC, id", gid, uid), [])
    e = discord.Embed(title="💰 Money", colour=GREEN)
    if acc:
        active = next((ch for ch in chars if ch["active"]), None)
        label = f" — {active['name']}" if active else ""
        e.description = "🧊 **Account frozen**" if acc["frozen"] else None
        e.add_field(name=f"💵 Wallet{label}", value=m(acc["wallet"]))
        e.add_field(name=f"🏦 Bank{label}", value=m(acc["bank"]))
        total = acc["wallet"] + acc["bank"] + sum(ch["wallet"] + ch["bank"] for ch in chars if not ch["active"])
        e.add_field(name="Σ All characters", value=m(total))
        parked = [f"**{ch['name']}**{' (main)' if ch['is_main'] else ''} — 💵 {m(ch['wallet'])} · 🏦 {m(ch['bank'])}"
                  for ch in chars if not ch["active"]]
        if parked:
            e.add_field(name="🎭 Other characters", value=_clip(parked), inline=False)
        e.add_field(name="Account opened", value=_t(acc["created_at"], "d"))
    else:
        e.description = "No bank account yet."
    pay = await _safe(q("SELECT o.name, p.amount, p.last_paid FROM payroll p JOIN orgs o ON o.id=p.org_id WHERE p.user_id=%s AND o.guild_id=%s",
                        uid, gid), [])
    if pay:
        e.add_field(name="💼 Payroll", value=_clip([f"**{p['name']}** — {m(p['amount'])} · last {_t(p['last_paid'], 'd')}" for p in pay]),
                    inline=False)
    embeds.append(e)

    # ───── 🎒 inventory ─────
    items = await _safe(q("SELECT name, qty FROM user_items WHERE guild_id=%s AND user_id=%s AND qty>0 ORDER BY name", gid, uid), [])
    e = discord.Embed(title=f"🎒 Inventory ({sum(i['qty'] for i in items)} items)", colour=BLUE,
                      description=_clip([f"• {i['name']} ×{i['qty']}" for i in items], 4000, "Empty"))
    for ch in chars:
        if ch["active"]:
            continue
        ci = await _safe(q("SELECT name, qty FROM character_items WHERE char_id=%s AND qty>0 ORDER BY name", ch["id"]), [])
        if ci:
            e.add_field(name=f"🎭 {ch['name']}", value=_clip([f"• {i['name']} ×{i['qty']}" for i in ci]), inline=False)
    grants = await _safe(q("SELECT role_id, item, expires_at FROM role_grants WHERE guild_id=%s AND user_id=%s AND expires_at > UTC_TIMESTAMP()",
                           gid, uid), [])
    if grants:
        e.add_field(name="⏳ Timed roles", value=_clip([f"<@&{g['role_id']}> ({g['item'] or 'item'}) — ends {_t(g['expires_at'])}"
                                                      for g in grants]), inline=False)
    embeds.append(e)

    # ───── 🏢 businesses & 🏠 properties ─────
    e = discord.Embed(title="🏢 Businesses & 🏠 Properties", colour=GOLD)
    owned = await _safe(q("SELECT id, kind, name, balance FROM orgs WHERE guild_id=%s AND owner_id=%s ORDER BY kind, name", gid, uid), [])
    e.add_field(name=f"👑 Owns ({len(owned)})", inline=False,
                value=_clip([f"{'🏢' if o['kind'] == 'business' else '🏛️'} **{o['name']}** — {m(o['balance'])}" for o in owned], 1024, "Nothing"))
    if member:
        rids = [r.id for r in member.roles]
        if rids:
            ph = ",".join(["%s"] * len(rids))
            mem = await _safe(q(f"SELECT o.kind, o.name, MIN(r.level) lvl FROM org_roles r JOIN orgs o ON o.id=r.org_id "
                                f"WHERE o.guild_id=%s AND r.role_id IN ({ph}) AND (o.owner_id IS NULL OR o.owner_id<>%s) "
                                f"GROUP BY o.id ORDER BY o.kind, o.name", gid, *rids, uid), [])
            if mem:
                e.add_field(name=f"👥 Member of ({len(mem)})", inline=False,
                            value=_clip([f"{'🏢' if x['kind'] == 'business' else '🏛️'} {x['name']} — {x['lvl']}" for x in mem]))
    props = await _safe(q("SELECT category, name, price FROM properties WHERE guild_id=%s AND owner_id=%s ORDER BY category, name", gid, uid), [])
    e.add_field(name=f"🏠 Properties ({len(props)})", inline=False,
                value=_clip([f"**{p['name']}** · {p['category']}" + (f" · {m(p['price'])}" if p["price"] else "") for p in props], 1024, "None"))
    reqs = await _safe(q("SELECT kind, title, status, created_at FROM req_requests WHERE guild_id=%s AND user_id=%s ORDER BY id DESC LIMIT 5",
                         gid, uid), [])
    if reqs:
        e.add_field(name="📨 Recent requests", inline=False,
                    value=_clip([f"{r['kind']} · **{r['title']}** · {r['status']} · {_t(r['created_at'], 'd')}" for r in reqs]))
    embeds.append(e)

    # ───── ⏱️ playtime & activity ─────
    e = discord.Embed(title="⏱️ Playtime & activity", colour=BLUE)
    t = _now()
    day0 = t.replace(hour=0, minute=0, second=0)
    week0 = day0 - dt.timedelta(days=day0.weekday())
    gta = await _safe(_gta(gid, uid, day0, week0, t), None)
    if gta:
        e.add_field(name="🎮 GTA V", inline=False, value=gta)
    pat = await _safe(q1("SELECT COUNT(*) n, COALESCE(SUM(a.seconds),0) s, MAX(p.started_at) last FROM patrol_attendance a "
                         "JOIN patrols p ON p.id=a.patrol_id WHERE p.guild_id=%s AND a.user_id=%s", gid, uid))
    if pat:
        e.add_field(name="🚓 Patrols", value=f"{pat['n']} attended · {_hm(pat['s'])}" + (f"\nLast {_t(pat['last'])}" if pat["last"] else ""))
    act = await _safe(q1("SELECT COALESCE(SUM(uses),0) uses, MAX(last_seen) seen, "
                         "SUM(CASE WHEN day >= UTC_DATE() - INTERVAL 30 DAY THEN 1 ELSE 0 END) d30 "
                         "FROM user_activity WHERE guild_id=%s AND user_id=%s", gid, uid))
    if act:
        last = await _safe(q1("SELECT last_action FROM user_activity WHERE guild_id=%s AND user_id=%s ORDER BY last_seen DESC LIMIT 1", gid, uid))
        e.add_field(name="🤖 Bot use", value=f"{act['uses']} commands · active {act['d30'] or 0}/30 days\n"
                    f"Last {_t(act['seen'])}" + (f" (`{last['last_action']}`)" if last and last.get("last_action") else ""))
    tk = await _safe(q1("SELECT COUNT(*) n, SUM(status='open') o FROM tickets WHERE guild_id=%s AND user_id=%s", gid, uid))
    if tk:
        e.add_field(name="🎫 Tickets", value=f"{tk['n']} total · {int(tk['o'] or 0)} open")
    apps = await _safe(q("SELECT form_name, status, created_at FROM applications WHERE guild_id=%s AND user_id=%s ORDER BY id DESC LIMIT 5",
                         gid, uid), [])
    if apps:
        e.add_field(name="📝 Applications", inline=False,
                    value=_clip([f"**{a['form_name']}** · {a['status']} · {_t(a['created_at'], 'd')}" for a in apps]))
    embeds.append(e)

    # ───── 🧾 owed & history ─────
    e = discord.Embed(title="🧾 Owed & history", colour=GOLD)
    cit = await _safe(q1("SELECT SUM(status='unpaid') n, COALESCE(SUM(CASE WHEN status='unpaid' THEN amount+late_fee END),0) s, "
                         "COUNT(*) total FROM citations WHERE guild_id=%s AND user_id=%s", gid, uid))
    if cit:
        e.add_field(name="🚨 Citations", value=f"{int(cit['n'] or 0)} unpaid ({m(cit['s'])})\n{cit['total']} all-time")
    bills = await _safe(q1("SELECT COUNT(*) n, COALESCE(SUM(amount+late_fee),0) s FROM util_bills WHERE guild_id=%s AND user_id=%s "
                           "AND status<>'paid'", gid, uid))
    accts = await _safe(q1("SELECT COUNT(*) n FROM util_accounts WHERE guild_id=%s AND user_id=%s AND status='active'", gid, uid))
    if bills is not None:
        e.add_field(name="💡 Utilities", value=f"{(accts or {}).get('n', 0)} active plans\n{bills['n']} unpaid bills ({m(bills['s'])})")
    hosp = await _safe(q1("SELECT admitted_at, reason, accrued, paid FROM hospital_stays WHERE guild_id=%s AND user_id=%s "
                          "AND discharged_at IS NULL ORDER BY id DESC LIMIT 1", gid, uid))
    try:
        from cogs.discipline import summary as disc_summary, MAX_STRIKES
        ds = await disc_summary(gid, uid)
        txt = f"**{ds['strikes']}/{MAX_STRIKES}** strikes · {ds['warnings']} warnings · {ds['suspensions']} suspensions"
        if ds["suspended"]:
            txt += f"\n⏸️ **Suspended** until {_t(ds['suspended']['ends_at'], 'F')} — {ds['suspended']['reason'][:80]}"
        for r in ds["last"][:3]:
            txt += f"\n{'~~' if not r['active'] else ''}{r['kind']} {_t(r['created_at'], 'd')} — {r['reason'][:60]}{'~~' if not r['active'] else ''}"
        e.add_field(name="⚖️ Discipline", value=txt[:1024], inline=False)
    except Exception:
        logger.exception("discipline summary failed")
    e.add_field(name="🏥 Hospital", value=(f"**Admitted** {_t(hosp['admitted_at'])}\n{hosp['reason'] or ''}\nOwes {m(hosp['accrued'] - hosp['paid'])}"
                                         if hosp else "Not admitted"))
    led = await _safe(q("SELECT ts, kind, amount, src_type, src_id, dst_type, dst_id, memo FROM ledger WHERE guild_id=%s AND "
                        "((src_id=%s AND src_type IN ('wallet','bank','cwallet','cbank')) OR (dst_id=%s AND dst_type IN ('wallet','bank','cwallet','cbank'))) "
                        "ORDER BY id DESC LIMIT 8", gid, uid, uid), [])
    if led:
        lines = [f"{'➕' if r['dst_id'] == uid else '➖'} {m(r['amount'])} · {r['kind']}" + (f" — {r['memo'][:40]}" if r["memo"] else "")
                 + f" · {_t(r['ts'])}" for r in led]
        e.add_field(name="📜 Recent transactions", value=_clip(lines), inline=False)
    embeds.append(e)
    embeds[-1].set_footer(text=f"Live from the bot database · {t:%Y-%m-%d %H:%M} UTC")
    return _fit(embeds)


def _fit(embeds, limit=5800):
    """Discord allows 6000 characters across all embeds in one message — shorten the longest blocks until it fits."""
    while sum(len(e) for e in embeds) > limit:
        best = None   # (length, embed, field index or -1 for description)
        for e in embeds:
            if e.description and (not best or len(e.description) > best[0]):
                best = (len(e.description), e, -1)
            for i, f in enumerate(e.fields):
                if not best or len(f.value) > best[0]:
                    best = (len(f.value), e, i)
        if not best or best[0] < 60:
            break
        n, e, i = best
        cut = max(40, n - (sum(len(x) for x in embeds) - limit) - 20)
        cut = min(cut, int(n * 0.7))
        text = (e.description if i < 0 else e.fields[i].value)
        text = text[:cut].rsplit("\n", 1)[0] + "\n…(trimmed)"
        if i < 0:
            e.description = text
        else:
            f = e.fields[i]
            e.set_field_at(i, name=f.name, value=text, inline=f.inline)
    return embeds


async def _gta(gid, uid, day0, week0, t):
    total = await _played(gid, uid, dt.datetime(2000, 1, 1))
    last = await q1("SELECT started_at, ended_at FROM gta_sessions WHERE guild_id=%s AND user_id=%s ORDER BY started_at DESC LIMIT 1", gid, uid)
    if not last:
        return "No sessions tracked yet"
    n = (await q1("SELECT COUNT(*) n FROM gta_sessions WHERE guild_id=%s AND user_id=%s", gid, uid))["n"]
    return (f"Today **{_hm(await _played(gid, uid, day0))}** · this week **{_hm(await _played(gid, uid, week0))}** · "
            f"30 days **{_hm(await _played(gid, uid, t - dt.timedelta(days=30)))}** · all-time **{_hm(total)}** ({n} sessions)\n"
            + ("🟢 Playing now" if not last["ended_at"] else f"Last played {_t(last['ended_at'])}"))


def card(embeds, *buttons) -> discord.ui.LayoutView:
    """All sections in ONE container (Components V2). Text limit for the whole message is 4000 characters."""
    embeds = _fit(embeds, 3500)
    thumb = embeds[0].thumbnail.url if embeds and embeds[0].thumbnail else None
    parts = []
    for i, e in enumerate(embeds):
        head = f"{'##' if i == 0 else '###'} {e.title}" + (f"\n{e.description}" if e.description else "")
        if i == 0 and thumb:
            parts.append(discord.ui.Section(discord.ui.TextDisplay(head), accessory=discord.ui.Thumbnail(thumb)))
        else:
            parts.append(discord.ui.TextDisplay(head))
        inline, block = [], []
        for f in e.fields:
            if f.inline and "\n" not in f.value:
                inline.append(f"**{f.name}:** {f.value}")
            else:
                block.append(f"**{f.name}**\n{f.value}")
        if inline:
            parts.append(discord.ui.TextDisplay("\n".join(inline)))
        if block:
            parts.append(discord.ui.TextDisplay("\n\n".join(block)))
        if e.footer and e.footer.text:
            parts.append(discord.ui.TextDisplay(f"-# {e.footer.text}"))
        parts.append(discord.ui.Separator())
    if buttons:
        parts.append(discord.ui.ActionRow(*buttons))
    elif parts:
        parts.pop()
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(discord.ui.Container(*parts, accent_colour=discord.Colour.blurple()))
    return v
