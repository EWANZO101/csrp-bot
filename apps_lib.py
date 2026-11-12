"""Application forms: requirement checks, review embed and decisions — shared by the bot cog and the web panel."""
import json, datetime as dt
import discord

import core
from core import q, q1, ex, cfg, emb, GREEN, RED, GOLD

QTYPES = ("short", "paragraph", "choice", "multi", "number", "yesno")


def jl(v, default):
    try:
        return json.loads(v) if v else default
    except Exception:
        return default


def form_dict(r) -> dict:
    return {"id": r["id"], "name": r["name"], "icon": r["icon"], "description": r["description"], "is_open": bool(r["is_open"]),
            "questions": jl(r["questions"], []), "requirements": jl(r["requirements"], {}),
            "review_channel": str(r["review_channel"] or ""), "reviewer_roles": jl(r["reviewer_roles"], []),
            "accept_roles": jl(r["accept_roles"], []), "remove_roles": jl(r["remove_roles"], []),
            "accept_message": r["accept_message"], "cooldown_days": r["cooldown_days"], "position": r["position"]}


async def check_requirements(member: discord.Member, form: dict) -> list[dict]:
    """Returns [{label, ok, detail}] for every requirement of the form."""
    req = form["requirements"] or {}
    c = await cfg(member.guild.id)
    roles = {r.id for r in member.roles}
    out = []
    g = member.guild
    for rid in req.get("roles_required") or []:
        role = g.get_role(int(rid))
        out.append({"label": f"Has the @{role.name if role else rid} role", "ok": int(rid) in roles})
    for rid in req.get("roles_blocked") or []:
        role = g.get_role(int(rid))
        out.append({"label": f"Does not have @{role.name if role else rid}", "ok": int(rid) not in roles})
    now = discord.utils.utcnow()
    if int(req.get("min_account_days") or 0):
        days = (now - member.created_at).days
        out.append({"label": f"Discord account at least {req['min_account_days']} days old", "ok": days >= int(req["min_account_days"]),
                    "detail": f"{days} days"})
    if int(req.get("min_member_days") or 0):
        days = (now - member.joined_at).days if member.joined_at else 0
        out.append({"label": f"In the server for at least {req['min_member_days']} days", "ok": days >= int(req["min_member_days"]),
                    "detail": f"{days} days"})
    if req.get("docs_ack") and c.get("docs_ack_role"):
        out.append({"label": "Read & acknowledged the server documents", "ok": c["docs_ack_role"] in roles,
                    "detail": "Press “I have read the documents” in the documents channel"})
    if req.get("no_unpaid_citations"):
        n = await q1("SELECT COUNT(*) n FROM citations WHERE guild_id=%s AND user_id=%s AND status='unpaid'", g.id, member.id)
        out.append({"label": "No unpaid citations", "ok": n["n"] == 0, "detail": f"{n['n']} unpaid" if n["n"] else None})
    for extra in req.get("custom") or []:
        if str(extra).strip():
            out.append({"label": str(extra), "ok": None, "detail": "Checked by staff"})
    # one pending at a time + cooldown after denial
    pend = await q1("SELECT id FROM applications WHERE guild_id=%s AND user_id=%s AND form_id=%s AND status='pending'",
                    g.id, member.id, form["id"])
    out.append({"label": "No application already pending", "ok": not pend, "detail": f"#{pend['id']} pending" if pend else None})
    if form.get("cooldown_days"):
        last = await q1("SELECT decided_at FROM applications WHERE guild_id=%s AND user_id=%s AND form_id=%s AND status='denied' "
                        "ORDER BY decided_at DESC LIMIT 1", g.id, member.id, form["id"])
        if last and last["decided_at"]:
            wait_until = last["decided_at"] + dt.timedelta(days=form["cooldown_days"])
            ok = dt.datetime.utcnow() >= wait_until
            out.append({"label": f"{form['cooldown_days']}-day wait after a denied application", "ok": ok,
                        "detail": None if ok else f"You can reapply after {wait_until:%Y-%m-%d}"})
    return out


def review_embed(c, form_name, app_row, answers, questions, user=None, status="pending", reviewer=None, reason=None):
    color = {"pending": GOLD, "accepted": GREEN, "denied": RED}[status]
    e = discord.Embed(title=f"📝 {form_name} — Application #{app_row}", color=color)
    if user:
        e.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
        e.add_field(name="Applicant", value=user.mention)
        e.add_field(name="Account created", value=discord.utils.format_dt(user.created_at, "R"))
        if getattr(user, "joined_at", None):
            e.add_field(name="Joined server", value=discord.utils.format_dt(user.joined_at, "R"))
    qmap = {q_["id"]: q_ for q_ in questions}
    total = 0
    for qid, ans in answers.items():
        label = qmap.get(qid, {}).get("label", qid)
        if isinstance(ans, list):
            ans = ", ".join(ans)
        val = str(ans if ans not in (None, "") else "—")
        if total + len(val) > 4500 or len(e.fields) >= 22:
            e.add_field(name="…", value="Answers truncated — see the full application in the web panel.", inline=False)
            break
        total += len(val)
        e.add_field(name=label[:256], value=val[:1024], inline=False)
    st = {"pending": "⏳ Pending review", "accepted": "✅ Accepted", "denied": "❌ Denied"}[status]
    if reviewer:
        st += f" by <@{reviewer}>"
    e.add_field(name="Status", value=st + (f"\n> {reason}" if reason else ""), inline=False)
    return e


def review_view(app_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"app:accept:{app_id}"))
    v.add_item(discord.ui.Button(label="Deny", emoji="❌", style=discord.ButtonStyle.danger, custom_id=f"app:deny:{app_id}"))
    return v


def can_review(member: discord.Member, form: dict, c: dict) -> bool:
    if core.is_admin(member, c, "applications"):
        return True
    rr = {int(x) for x in form.get("reviewer_roles") or [] if str(x).isdigit()}
    return any(r.id in rr for r in member.roles)


async def post_for_review(bot, guild: discord.Guild, form: dict, app_id: int, answers: dict, member):
    c = await cfg(guild.id)
    ch = guild.get_channel(int(form["review_channel"] or 0)) or guild.get_channel(c.get("log_channel") or 0)
    if not ch:
        return
    pings = " ".join(f"<@&{r}>" for r in form.get("reviewer_roles") or [])
    msg = await ch.send(content=pings or None, embed=review_embed(c, form["name"], app_id, answers, form["questions"], member),
                        view=review_view(app_id), allowed_mentions=discord.AllowedMentions(roles=True))
    await ex("UPDATE applications SET review_message=%s WHERE id=%s", msg.id, app_id)


async def decide(bot, guild: discord.Guild, app_id: int, accept: bool, reviewer_id: int, reason: str | None) -> str:
    """Accept/deny an application. Returns a short result note. Raises core.EconError on problems."""
    a = await q1("SELECT * FROM applications WHERE id=%s AND guild_id=%s", app_id, guild.id)
    if not a:
        raise core.EconError("Application not found.")
    if a["status"] != "pending":
        raise core.EconError(f"Already {a['status']}.")
    status = "accepted" if accept else "denied"
    n, _ = await ex("UPDATE applications SET status=%s, reviewer_id=%s, reason=%s, decided_at=UTC_TIMESTAMP() "
                    "WHERE id=%s AND status='pending'", status, reviewer_id, (reason or "")[:1000] or None, app_id)
    if not n:
        raise core.EconError("Someone else just reviewed it.")
    frow = await q1("SELECT * FROM app_forms WHERE id=%s", a["form_id"]) if a["form_id"] else None
    form = form_dict(frow) if frow else {"questions": [], "accept_roles": [], "remove_roles": [], "accept_message": None,
                                         "review_channel": ""}
    c = await cfg(guild.id)
    note = ""
    member = guild.get_member(a["user_id"])
    if member is None:
        try:
            member = await guild.fetch_member(a["user_id"])
        except discord.HTTPException:
            member = None
    if accept and member:
        add = [guild.get_role(int(r)) for r in form["accept_roles"] if str(r).isdigit()]
        rem = [guild.get_role(int(r)) for r in form["remove_roles"] if str(r).isdigit()]
        try:
            if [r for r in add if r]:
                await member.add_roles(*[r for r in add if r], reason=f"Application #{app_id} accepted")
            if [r for r in rem if r and r in member.roles]:
                await member.remove_roles(*[r for r in rem if r and r in member.roles], reason=f"Application #{app_id} accepted")
        except discord.Forbidden:
            note = "⚠️ Couldn't change roles — move the bot's role higher."
    elif accept:
        note = "⚠️ Applicant isn't in the server anymore."
    # DM applicant
    if accept:
        dm_e = emb(f"✅ Your {a['form_name']} application was accepted!", form.get("accept_message") or
                   f"Welcome aboard! Check {guild.name} for next steps.", GREEN)
    else:
        dm_e = emb(f"❌ Your {a['form_name']} application was denied", reason or "Thanks for applying — you may reapply later.", RED)
    if reason and accept:
        dm_e.add_field(name="Note from staff", value=reason[:1024])
    await core.dm(bot, a["user_id"], dm_e)
    # update review message
    if a["review_message"]:
        ch = guild.get_channel(int(form.get("review_channel") or 0)) or guild.get_channel(c.get("log_channel") or 0)
        if ch:
            try:
                msg = await ch.fetch_message(a["review_message"])
                user = member or await bot.fetch_user(a["user_id"])
                await msg.edit(embed=review_embed(c, a["form_name"], app_id, jl(a["answers"], {}), form["questions"], user,
                                                  status, reviewer_id, reason), view=None)
            except discord.HTTPException:
                pass
    await core.log(bot, guild.id, emb(f"📝 Application {status}", f"#{app_id} {a['form_name']} · <@{a['user_id']}> by <@{reviewer_id}>",
                                      GREEN if accept else RED))
    return note
