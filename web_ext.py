"""Extra web panel routes: documents (public pages + builder), tickets, applications, server setup & branding."""
import os, re, json, html, base64, datetime as dt
from aiohttp import web

import core
from core import q, q1, ex, cfg, set_cfg, EconError, is_admin
import docs_lib
import apps_lib

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")


def _iso(d):
    return d.replace(tzinfo=dt.timezone.utc).isoformat() if d else None


def _ids(v) -> list[str]:
    if isinstance(v, str):
        v = v.split(",")
    return [str(x).strip() for x in (v or []) if str(x).strip().isdigit()]


def register(p):
    """Attach routes to a web.Panel instance `p`."""
    r = p.app.router
    data_dir = os.getenv("DATA_DIR", os.path.join(BASE, "data"))
    brand_dir = os.path.join(data_dir, "brand")
    os.makedirs(brand_dir, exist_ok=True)

    async def viewer(request):
        """(member|None, is_admin) without raising."""
        s = core_unsign(request)
        if not s or not p.ready:
            return None, False
        m = await p.member(int(s["uid"]))
        if not m:
            return None, set()
        return m, core.staff_perms(m, await cfg(p.gid))

    def core_unsign(request):
        from web import unsign
        return unsign(request.cookies.get("eb_session"))

    async def brand():
        c = await cfg(p.gid) if p.gid else dict(core.DEFAULTS)
        col = c.get("brand_color") or "#38b6ff"
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", col)
        rgb = ",".join(str(int(m.group(1)[i:i + 2], 16)) for i in (0, 2, 4)) if m else "56,182,255"
        return {"name": c.get("brand_name") or "Panel", "full": c.get("brand_full") or "", "color": "#" + (m.group(1) if m else "38b6ff"),
                "rgb": rgb}

    def shell(b, title, body, extra_head=""):
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#060a12">
<title>{html.escape(title)} · {html.escape(b['name'])}</title><link rel="icon" href="/brand/logo">
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/app.css?v=8"><style>:root{{--brand:{b['color']};--brand-rgb:{b['rgb']}}}</style>{extra_head}</head>
<body><div class="backdrop"></div><div class="watermark"></div>
<nav class="topnav"><a class="brand" href="/docs"><img src="/brand/logo" alt=""><span><b>{html.escape(b['name'])}</b><small>{html.escape(b['full'])}</small></span></a>
<div class="sp"></div><a class="btn ghost sm" href="/docs">📚 Documents</a><a class="btn sm" href="/">Open panel</a></nav>{body}</body></html>"""

    # ───────── branding assets ─────────
    async def logo(request):
        custom = os.path.join(brand_dir, "logo.png")
        return web.FileResponse(custom if os.path.exists(custom) else os.path.join(STATIC, "brand", "logo.png"),
                                headers={"Cache-Control": "public, max-age=300"})

    async def brand_api(request):
        return web.json_response(await brand())

    async def upload_logo(request):
        await p.session(request, perm="setup")
        b = await p.body(request)
        m = re.match(r"data:image/(png|jpe?g|webp);base64,(.+)", b.get("data") or "", re.S)
        if not m:
            raise EconError("Upload a PNG, JPG or WEBP image.")
        raw = base64.b64decode(m.group(2))
        if len(raw) > 4 * 1024 * 1024:
            raise EconError("Image must be under 4 MB.")
        with open(os.path.join(brand_dir, "logo.png"), "wb") as f:
            f.write(raw)
        return web.json_response({"ok": True})

    # ───────── public documents ─────────
    async def visible_docs(request):
        m, adm = await viewer(request)
        rows = await q("SELECT id,slug,title,summary,category,icon,kind,url,visibility,updated_at,position,in_panel "
                       "FROM documents WHERE guild_id=%s ORDER BY position, title", p.gid)
        out = []
        for d in rows:
            if d["visibility"] == "staff" and not adm:   # any staff member
                continue
            if d["visibility"] == "members" and not m:
                continue
            out.append(d)
        return out, m, adm

    async def docs_index(request):
        b = await brand()
        docs, m, adm = await visible_docs(request)
        cats = {}
        for d in docs:
            cats.setdefault(d["category"] or "General", []).append(d)
        parts = []
        for cat, items in cats.items():
            parts.append(f'<div class="cat-h">{html.escape(cat)}</div><div class="doc-grid">')
            for d in items:
                link = html.escape(d["url"]) if d["kind"] == "link" else f"/docs/{html.escape(d['slug'])}"
                tgt = ' target="_blank" rel="noopener"' if d["kind"] == "link" else ""
                parts.append(f'<a class="doc-card" href="{link}"{tgt}><div class="di">{html.escape(d["icon"] or "📄")}</div>'
                             f'<b>{html.escape(d["title"])}</b><p>{html.escape(d["summary"] or "")}</p>'
                             f'<div class="meta">{"<span class=pill>external link</span>" if d["kind"] == "link" else "Updated " + d["updated_at"].strftime("%d %b %Y")}'
                             f'{" · <span class=pill>members</span>" if d["visibility"] == "members" else ""}'
                             + (' · <span class="pill gold">staff</span>' if d["visibility"] == "staff" else "") + '</div>'
                             + ('<span class="ext">↗</span>' if d["kind"] == "link" else "") + '</a>')
            parts.append("</div>")
        if not docs:
            parts.append('<div class="card empty"><span class="e-ico">📭</span>No documents published yet.</div>')
        login = "" if m else '<p class="mute" style="margin-top:22px">Some documents are only visible to members — <a href="/login">log in with Discord</a>.</p>'
        body = f"""<main style="max-width:1180px;margin:0 auto;padding:34px 24px 80px">
<div class="hero"><img src="/brand/logo" alt=""><div><h2>{html.escape(b['full'] or b['name'])} Documents</h2>
<p>Standard operating procedures, penal codes and everything you need before you RP.</p></div></div>
{''.join(parts)}{login}</main>"""
        return web.Response(text=shell(b, "Documents", body), content_type="text/html")

    async def doc_page(request):
        b = await brand()
        slug = request.match_info["slug"]
        d = await q1("SELECT * FROM documents WHERE guild_id=%s AND slug=%s", p.gid, slug)
        m, adm = await viewer(request)
        if not d or (d["visibility"] == "staff" and not adm):
            return web.Response(text=shell(b, "Not found", '<div class="center"><div class="card auth"><h1>404</h1><p>That document doesn\'t exist.</p><a class="btn" href="/docs">All documents</a></div></div>'),
                                content_type="text/html", status=404)
        if d["visibility"] == "members" and not m:
            return web.Response(text=shell(b, d["title"], f'<div class="center"><div class="card auth"><img class="logo" src="/brand/logo" alt=""><h1>Members only</h1><p>Log in with Discord to read <b>{html.escape(d["title"])}</b>.</p><a class="btn discord-btn" href="/login?next=/docs/{html.escape(slug)}">Login with Discord</a></div></div>'),
                                content_type="text/html", status=401)
        if d["kind"] == "link":
            raise web.HTTPFound(d["url"])
        body_html, toc = docs_lib.add_heading_ids(d["content"] or "")
        has_table = "<table" in body_html
        toc_html = "".join(f'<a class="l{t["level"]}" href="#{t["id"]}">{html.escape(t["text"][:70])}</a>' for t in toc)
        edit = f'<a class="btn ghost sm no-print" href="/#doc-edit-{d["id"]}">✏️ Edit</a>' if "documents" in adm else ""
        search = ('<input class="in doc-search no-print" id="tsearch" placeholder="Search this table…" '
                  'oninput="filterRows(this.value)">') if has_table else ""
        body = f"""<div class="doc-wrap {'' if len(toc) > 1 else 'no-toc'}">
{f'<aside class="toc"><div class="nav-h" style="padding-left:10px">On this page</div>{toc_html}</aside>' if len(toc) > 1 else ''}
<article><header class="doc-head"><div class="crumbs"><a href="/docs">Documents</a> / {html.escape(d['category'] or 'General')}</div>
<h1>{html.escape(d['icon'] or '')} {html.escape(d['title'])}</h1>{f'<p class="mute" style="margin:8px 0 0;font-size:15px">{html.escape(d["summary"])}</p>' if d['summary'] else ''}
<div class="meta"><span>Last updated {d['updated_at'].strftime('%d %B %Y')}</span>
<span class="no-print" style="display:flex;gap:8px;margin-left:auto">{search}<button class="btn ghost sm" onclick="print()">🖨️ Print</button>{edit}</span></div></header>
<div class="prose">{body_html}</div></article></div>
<script>
function filterRows(v){{v=v.toLowerCase();document.querySelectorAll('.prose tbody tr').forEach(tr=>{{if(tr.querySelector('.pc-section'))return;tr.classList.toggle('hide',v&&!tr.textContent.toLowerCase().includes(v))}})}}
const links=[...document.querySelectorAll('.toc a')];if(links.length){{const obs=new IntersectionObserver(es=>{{es.forEach(e=>{{if(e.isIntersecting){{links.forEach(a=>a.classList.toggle('on',a.getAttribute('href')==='#'+e.target.id))}}}})}},{{rootMargin:'-80px 0px -70% 0px'}});document.querySelectorAll('.prose h1[id],.prose h2[id],.prose h3[id]').forEach(h=>obs.observe(h))}}
</script>"""
        return web.Response(text=shell(b, d["title"], body), content_type="text/html")

    # ───────── documents API (builder) ─────────
    async def docs_list(request):
        await p.session(request, perm="documents")
        rows = await q("SELECT id,slug,title,summary,category,icon,kind,url,visibility,position,in_panel,source_url,updated_at,updated_by,"
                       "LENGTH(content) size FROM documents WHERE guild_id=%s ORDER BY position, title", p.gid)
        return web.json_response([{**r, "updated_at": _iso(r["updated_at"]), "updated_by": str(r["updated_by"] or "")} for r in rows])

    async def doc_get(request):
        await p.session(request, perm="documents")
        d = await q1("SELECT * FROM documents WHERE guild_id=%s AND id=%s", p.gid, int(request.match_info["id"]))
        if not d:
            raise EconError("Not found")
        return web.json_response({**d, "updated_at": _iso(d["updated_at"]), "created_at": _iso(d["created_at"]),
                                  "created_by": str(d["created_by"] or ""), "updated_by": str(d["updated_by"] or "")})

    async def doc_save(request):
        m, adm, c = await p.session(request, perm="documents")
        b = await p.body(request)
        title = (b.get("title") or "").strip()[:120]
        if not title:
            raise EconError("Give the document a title.")
        kind = "link" if b.get("kind") == "link" else "page"
        url = (b.get("url") or "").strip()[:500]
        if kind == "link" and not re.match(r"^https?://", url):
            raise EconError("Links need a full http(s):// URL.")
        slug = docs_lib.slugify(b.get("slug") or title)
        vis = b.get("visibility") if b.get("visibility") in ("public", "members", "staff") else "public"
        content = docs_lib.sanitise(b.get("content") or "") if kind == "page" else (b.get("content") and docs_lib.sanitise(b["content"]))
        vals = dict(slug=slug, title=title, summary=(b.get("summary") or "").strip()[:300] or None,
                    category=(b.get("category") or "").strip()[:60] or None, icon=(b.get("icon") or "").strip()[:16] or None,
                    kind=kind, url=url or None, content=content, visibility=vis, in_panel=bool(b.get("in_panel", True)),
                    source_url=(b.get("source_url") or None))
        did = int(b.get("id") or 0)
        clash = await q1("SELECT id FROM documents WHERE guild_id=%s AND slug=%s AND id<>%s", p.gid, slug, did)
        if clash:
            vals["slug"] = f"{slug}-{did or 'new'}"[:64] if did else f"{slug}-{int(dt.datetime.utcnow().timestamp()) % 10000}"
        if did:
            await ex(f"UPDATE documents SET {', '.join(f'{k}=%s' for k in vals)}, updated_by=%s WHERE id=%s AND guild_id=%s",
                     *vals.values(), m.id, did, p.gid)
        else:
            pos = await q1("SELECT COALESCE(MAX(position),0)+1 n FROM documents WHERE guild_id=%s", p.gid)
            _, did = await ex(f"INSERT INTO documents (guild_id,{','.join(vals)},position,created_by,updated_by) "
                              f"VALUES (%s,{','.join(['%s'] * len(vals))},%s,%s,%s)", p.gid, *vals.values(), pos["n"], m.id, m.id)
        await refresh_panel("Documents")
        return web.json_response({"ok": True, "id": did, "slug": vals["slug"]})

    async def doc_delete(request):
        await p.session(request, perm="documents")
        await ex("DELETE FROM documents WHERE guild_id=%s AND id=%s", p.gid, int(request.match_info["id"]))
        await refresh_panel("Documents")
        return web.json_response({"ok": True})

    async def doc_order(request):
        await p.session(request, perm="documents")
        b = await p.body(request)
        for i, did in enumerate(b.get("ids") or []):
            await ex("UPDATE documents SET position=%s WHERE guild_id=%s AND id=%s", i, p.gid, int(did))
        await refresh_panel("Documents")
        return web.json_response({"ok": True})

    async def doc_import(request):
        await p.session(request, perm="documents")
        b = await p.body(request)
        try:
            title, body = await docs_lib.import_google(b.get("url") or "")
        except ValueError as e:
            raise EconError(str(e))
        return web.json_response({"title": title, "content": body, "source_url": b.get("url")})

    async def refresh_panel(cog_name):
        cog = p.bot.get_cog(cog_name) if p.bot else None
        if cog and p.guild:
            try:
                return await cog.post_panel(p.guild)
            except Exception as e:
                return str(e)

    async def post_panel(request):
        await p.session(request, perm="setup")
        b = await p.body(request)
        which = {"documents": "Documents", "tickets": "Tickets", "applications": "Applications"}.get(b.get("which"))
        if not which:
            raise EconError("Unknown panel")
        cog = p.bot.get_cog(which)
        if not cog:
            raise EconError(f"The {which.lower()} feature is turned off.")
        err = await cog.post_panel(p.guild, force_new=bool(b.get("new")))
        if err:
            raise EconError(err)
        return web.json_response({"ok": True})

    # ───────── tickets ─────────
    async def tickets_list(request):
        await p.session(request, perm="tickets")
        st = request.query.get("status", "open")
        cond = "" if st == "all" else " AND status=%s"
        args = [p.gid] + ([] if st == "all" else [st])
        rows = await q(f"SELECT id,channel_id,user_id,category,subject,status,claimed_by,created_at,closed_at,closed_by,close_reason "
                       f"FROM tickets WHERE guild_id=%s{cond} ORDER BY id DESC LIMIT 300", *args)
        names = await p.whos([x["user_id"] for x in rows] + [x["claimed_by"] for x in rows] + [x["closed_by"] for x in rows])
        return web.json_response([{**t, "channel_id": str(t["channel_id"] or ""), "user": names.get(str(t["user_id"])),
                                   "claimed": names.get(str(t["claimed_by"])) if t["claimed_by"] else None,
                                   "closer": names.get(str(t["closed_by"])) if t["closed_by"] else None,
                                   "user_id": str(t["user_id"]), "claimed_by": str(t["claimed_by"] or ""), "closed_by": str(t["closed_by"] or ""),
                                   "created_at": _iso(t["created_at"]), "closed_at": _iso(t["closed_at"])} for t in rows])

    async def ticket_get(request):
        await p.session(request, perm="tickets")
        t = await q1("SELECT * FROM tickets WHERE guild_id=%s AND id=%s", p.gid, int(request.match_info["id"]))
        if not t:
            raise EconError("Not found")
        return web.json_response({"id": t["id"], "category": t["category"], "subject": t["subject"], "status": t["status"],
                                  "user": await p.who(t["user_id"]), "transcript": t["transcript"] or "",
                                  "created_at": _iso(t["created_at"]), "closed_at": _iso(t["closed_at"]),
                                  "close_reason": t["close_reason"], "channel_id": str(t["channel_id"] or "")})

    # ───────── server setup (features, channels, branding) ─────────
    async def guild_setup(request):
        await p.session(request, perm="setup")
        c = await cfg(p.gid)
        g = p.guild
        feats = {**core.DEFAULT_FEATURES, **(p.state.settings.get("features") or {})}
        keys = ["log_channel", "docs_channel", "docs_ack_role", "docs_title", "docs_text", "ticket_panel_channel", "ticket_category",
                "ticket_log_channel", "ticket_staff_roles", "ticket_max_open", "ticket_types", "ticket_panel_title", "ticket_panel_text",
                "apps_channel", "apps_title", "apps_text", "brand_name", "brand_full", "brand_color", "owner_role", "coowner_role",
                "onboarded"]
        return web.json_response({
            "features": feats, "feature_info": {k: {"name": v[0], "cmds": v[1]} for k, v in core.FEATURES.items()},
            "config": {k: (str(c[k]) if isinstance(c.get(k), int) and not isinstance(c.get(k), bool) and c[k] > 10**15 else c.get(k))
                       for k in keys} | {"ticket_staff_roles": [str(x) for x in c.get("ticket_staff_roles") or []]},
            "channels": [{"id": str(ch.id), "name": ch.name, "cat": ch.category.name if ch.category else ""} for ch in g.text_channels],
            "categories": [{"id": str(ch.id), "name": ch.name} for ch in g.categories],
            "roles": [{"id": str(r.id), "name": r.name, "color": str(r.color) if r.color.value else None}
                      for r in reversed(g.roles) if not r.is_default() and not r.managed],
            "bot_top_role": g.me.top_role.position if g.me else 0,
            "role_problems": role_problems(g, c),
        })

    def role_problems(g, c):
        """Roles the bot is asked to hand out but sits below."""
        if not g.me:
            return []
        top = g.me.top_role.position
        ids = {c.get("docs_ack_role")}
        return sorted({r.name for r in (g.get_role(int(i)) for i in ids if i) if r and r.position >= top})

    async def guild_autocreate(request):
        """One click: create category, channels, a 'read the docs' role, and wire them all up."""
        import discord
        m, adm, c = await p.session(request, perm="setup")
        g = p.guild
        me = g.me
        if not me.guild_permissions.manage_channels or not me.guild_permissions.manage_roles:
            raise EconError("The bot needs Manage Channels and Manage Roles — re-invite it with the invite link in Settings.")
        name = c.get("brand_name") or "Bot"
        made, changes = [], {}
        staff = [g.get_role(int(r)) for r in c.get("ticket_staff_roles") or []]
        admins = [r for r in g.roles if r.permissions.administrator and not r.managed] + [r for r in staff if r]
        ro = {g.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False,
                                                          create_public_threads=False, read_message_history=True),
              me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_messages=True)}
        hidden = {g.default_role: discord.PermissionOverwrite(view_channel=False),
                  me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                                                  read_message_history=True)}
        for r in admins:
            hidden[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        cat = discord.utils.get(g.categories, name=f"📋 {name} Info")
        if not cat:
            cat = await g.create_category(f"📋 {name} Info", reason="Bot setup")
            made.append(cat.name)
        staffcat = discord.utils.get(g.categories, name=f"🔒 {name} Staff")
        if not staffcat:
            staffcat = await g.create_category(f"🔒 {name} Staff", overwrites=hidden, reason="Bot setup")
            made.append(staffcat.name)

        async def chan(key, cname, category, ow, topic):
            if c.get(key) and g.get_channel(int(c[key])):
                return
            ch = discord.utils.get(category.text_channels, name=cname) or \
                await g.create_text_channel(cname, category=category, overwrites=ow, topic=topic, reason="Bot setup")
            changes[key] = ch.id
            made.append("#" + ch.name)
        await chan("docs_channel", "📚・documents", cat, ro, "Server documents — read before you RP")
        await chan("ticket_panel_channel", "🎫・support", cat, ro, "Open a support ticket")
        await chan("apps_channel", "📝・applications", cat, ro, "Apply to join a department")
        await chan("log_channel", "📒・bot-logs", staffcat, hidden, "Economy & staff action logs")
        await chan("ticket_log_channel", "🗂・ticket-transcripts", staffcat, hidden, "Closed ticket transcripts")
        if not (c.get("ticket_category") and g.get_channel(int(c["ticket_category"]))):
            tc = discord.utils.get(g.categories, name="🎫 Tickets") or await g.create_category("🎫 Tickets", reason="Bot setup")
            changes["ticket_category"] = tc.id
            made.append(tc.name)
        if not (c.get("docs_ack_role") and g.get_role(int(c["docs_ack_role"]))):
            role = discord.utils.get(g.roles, name="✅ Read the documents") or \
                await g.create_role(name="✅ Read the documents", reason="Bot setup — given by the documents panel")
            changes["docs_ack_role"] = role.id
            made.append("@" + role.name)
        # applications without their own review channel post to the logs channel by default
        await set_cfg(p.gid, **changes, onboarded=True)
        errors = {}
        for n in ("Documents", "Tickets", "Applications"):
            e = await refresh_panel(n)
            if e:
                errors[n] = e
        await core.log(p.bot, p.gid, core.emb("🛠️ Channels created (web)", ", ".join(made) or "nothing new", core.GREEN))
        return web.json_response({"ok": True, "made": made, "panel_errors": errors})

    async def guild_setup_save(request):
        m, adm, c = await p.session(request, perm="setup")
        b = await p.body(request)
        ch = {}
        for k in ("log_channel", "docs_channel", "docs_ack_role", "ticket_panel_channel", "ticket_category", "ticket_log_channel",
                  "apps_channel", "owner_role", "coowner_role"):
            if k in b:
                ch[k] = int(b[k]) if str(b[k] or "").isdigit() else None
        for k in ("docs_title", "docs_text", "ticket_panel_title", "ticket_panel_text", "apps_title", "apps_text",
                  "brand_name", "brand_full"):
            if k in b:
                ch[k] = str(b[k] or "").strip()[:1500]
        if "brand_color" in b and re.fullmatch(r"#[0-9a-fA-F]{6}", str(b["brand_color"])):
            ch["brand_color"] = b["brand_color"]
        if "ticket_max_open" in b:
            ch["ticket_max_open"] = max(1, min(10, int(b["ticket_max_open"] or 2)))
        if "ticket_staff_roles" in b:
            ch["ticket_staff_roles"] = [int(x) for x in _ids(b["ticket_staff_roles"])]
        if "ticket_types" in b:
            types = []
            for t in (b["ticket_types"] or [])[:25]:
                name = str(t.get("name") or "").strip()[:80]
                if name:
                    types.append({"name": name, "emoji": str(t.get("emoji") or "").strip()[:8],
                                  "description": str(t.get("description") or "").strip()[:100]})
            ch["ticket_types"] = types
        if b.get("onboarded"):
            ch["onboarded"] = True
        if ch:
            await set_cfg(p.gid, **ch)
        restart = False
        if isinstance(b.get("features"), dict):
            new = {k: bool(b["features"].get(k)) for k in core.FEATURES}
            old = {**core.DEFAULT_FEATURES, **(p.state.settings.get("features") or {})}
            if new != old:
                p.state.settings["features"] = new
                p.state.save()
                restart = True
        errors = {}
        if restart:
            await p.state.start_bot()
        else:
            for name in ("Documents", "Tickets", "Applications"):
                e = await refresh_panel(name)
                if e:
                    errors[name] = e
        await core.log(p.bot, p.gid, core.emb("⚙️ Server setup updated (web)", f"by <@{m.id}>" + (" · bot restarting" if restart else ""),
                                              core.GOLD))
        return web.json_response({"ok": True, "restarting": restart, "panel_errors": errors})

    # ───────── applications ─────────
    async def forms_public(request):
        m, adm, c = await p.session(request)
        rows = await q("SELECT * FROM app_forms WHERE guild_id=%s ORDER BY position, name", p.gid)
        out = []
        for r in rows:
            f = apps_lib.form_dict(r)
            if not f["is_open"] and not adm:
                continue
            checks = await apps_lib.check_requirements(m, f)
            out.append({k: f[k] for k in ("id", "name", "icon", "description", "is_open", "questions")} |
                       {"checks": checks, "eligible": f["is_open"] and all(x["ok"] is not False for x in checks)})
        mine = await q("SELECT id, form_name, status, reason, created_at, decided_at FROM applications WHERE guild_id=%s AND user_id=%s "
                       "ORDER BY id DESC LIMIT 30", p.gid, m.id)
        return web.json_response({"forms": out, "mine": [{**a, "created_at": _iso(a["created_at"]), "decided_at": _iso(a["decided_at"])}
                                                          for a in mine]})

    async def form_submit(request):
        m, adm, c = await p.session(request)
        b = await p.body(request)
        r = await q1("SELECT * FROM app_forms WHERE guild_id=%s AND id=%s", p.gid, int(b.get("form_id") or 0))
        if not r:
            raise EconError("Form not found.")
        f = apps_lib.form_dict(r)
        if not f["is_open"]:
            raise EconError("This application is closed.")
        checks = await apps_lib.check_requirements(m, f)
        failed = [x["label"] for x in checks if x["ok"] is False]
        if failed:
            raise EconError("Requirements not met: " + "; ".join(failed))
        answers, raw = {}, b.get("answers") or {}
        for qq in f["questions"]:
            v = raw.get(qq["id"])
            if qq["type"] == "multi":
                v = [str(x)[:200] for x in (v or []) if str(x) in (qq.get("options") or [])]
            elif qq["type"] == "choice":
                v = str(v) if str(v or "") in (qq.get("options") or []) else ""
            elif qq["type"] == "yesno":
                v = {"yes": "Yes", "no": "No", True: "Yes", False: "No"}.get(v if isinstance(v, bool) else str(v or "").lower(), "")
            elif qq["type"] == "number":
                v = str(v or "").strip()
                if v and not re.fullmatch(r"-?\d+(\.\d+)?", v):
                    raise EconError(f"“{qq['label']}” must be a number.")
            else:
                v = str(v or "").strip()[: (4000 if qq["type"] == "paragraph" else 300)]
            if qq.get("required") and (v in ("", None, []) ):
                raise EconError(f"Please answer “{qq['label']}”.")
            answers[qq["id"]] = v
        _, aid = await ex("INSERT INTO applications (guild_id,form_id,form_name,user_id,answers) VALUES (%s,%s,%s,%s,%s)",
                          p.gid, f["id"], f["name"], m.id, json.dumps(answers))
        try:
            await apps_lib.post_for_review(p.bot, p.guild, f, aid, answers, m)
        except Exception as e:
            print(f"[apps] review post failed: {e!r}")
        await core.dm(p.bot, m.id, core.emb(f"📝 {f['name']} application received", f"Application #{aid} is waiting for review. "
                                            "You'll get a DM when staff decide.", core.BLUE))
        return web.json_response({"ok": True, "id": aid})

    async def forms_admin(request):
        await p.session(request, perm="applications")
        rows = await q("SELECT f.*, (SELECT COUNT(*) FROM applications a WHERE a.form_id=f.id AND a.status='pending') pending "
                       "FROM app_forms f WHERE guild_id=%s ORDER BY position, name", p.gid)
        return web.json_response([apps_lib.form_dict(r) | {"pending": r["pending"]} for r in rows])

    async def form_save(request):
        m, adm, c = await p.session(request, perm="applications")
        b = await p.body(request)
        name = (b.get("name") or "").strip()[:100]
        if not name:
            raise EconError("Form needs a name.")
        qs = []
        for i, qq in enumerate((b.get("questions") or [])[:40]):
            label = str(qq.get("label") or "").strip()[:250]
            if not label:
                continue
            t = qq.get("type") if qq.get("type") in apps_lib.QTYPES else "short"
            opts = [str(o).strip()[:100] for o in (qq.get("options") or []) if str(o).strip()][:25]
            if t in ("choice", "multi") and not opts:
                raise EconError(f"“{label}” needs at least one option.")
            qs.append({"id": str(qq.get("id") or f"q{i}")[:20], "label": label, "type": t, "required": bool(qq.get("required")),
                       "help": str(qq.get("help") or "")[:300], "options": opts})
        if not qs:
            raise EconError("Add at least one question.")
        req = b.get("requirements") or {}
        req = {"roles_required": _ids(req.get("roles_required")), "roles_blocked": _ids(req.get("roles_blocked")),
               "min_account_days": max(0, int(req.get("min_account_days") or 0)), "min_member_days": max(0, int(req.get("min_member_days") or 0)),
               "docs_ack": bool(req.get("docs_ack")), "no_unpaid_citations": bool(req.get("no_unpaid_citations")),
               "custom": [str(x).strip()[:200] for x in (req.get("custom") or []) if str(x).strip()][:10]}
        vals = dict(name=name, icon=(b.get("icon") or "").strip()[:16] or None, description=(b.get("description") or "")[:3000],
                    is_open=bool(b.get("is_open", True)), questions=json.dumps(qs), requirements=json.dumps(req),
                    review_channel=int(b["review_channel"]) if str(b.get("review_channel") or "").isdigit() else None,
                    reviewer_roles=json.dumps(_ids(b.get("reviewer_roles"))), accept_roles=json.dumps(_ids(b.get("accept_roles"))),
                    remove_roles=json.dumps(_ids(b.get("remove_roles"))), accept_message=(b.get("accept_message") or "")[:1500] or None,
                    cooldown_days=max(0, int(b.get("cooldown_days") or 0)))
        fid = int(b.get("id") or 0)
        if fid:
            await ex(f"UPDATE app_forms SET {', '.join(f'{k}=%s' for k in vals)} WHERE id=%s AND guild_id=%s", *vals.values(), fid, p.gid)
        else:
            _, fid = await ex(f"INSERT INTO app_forms (guild_id,{','.join(vals)}) VALUES (%s,{','.join(['%s'] * len(vals))})",
                              p.gid, *vals.values())
        await refresh_panel("Applications")
        return web.json_response({"ok": True, "id": fid})

    async def form_delete(request):
        await p.session(request, perm="applications")
        await ex("DELETE FROM app_forms WHERE guild_id=%s AND id=%s", p.gid, int(request.match_info["id"]))
        await refresh_panel("Applications")
        return web.json_response({"ok": True})

    async def apps_list(request):
        await p.session(request, perm="applications")
        st = request.query.get("status", "pending")
        cond = "" if st == "all" else " AND status=%s"
        args = [p.gid] + ([] if st == "all" else [st])
        rows = await q(f"SELECT * FROM applications WHERE guild_id=%s{cond} ORDER BY id DESC LIMIT 300", *args)
        names = await p.whos([a["user_id"] for a in rows] + [a["reviewer_id"] for a in rows])
        forms = {f["id"]: apps_lib.form_dict(f) for f in await q("SELECT * FROM app_forms WHERE guild_id=%s", p.gid)}
        out = []
        for a in rows:
            f = forms.get(a["form_id"])
            out.append({"id": a["id"], "form": a["form_name"], "form_id": a["form_id"], "status": a["status"], "reason": a["reason"],
                        "user": names.get(str(a["user_id"])), "reviewer": names.get(str(a["reviewer_id"])) if a["reviewer_id"] else None,
                        "created_at": _iso(a["created_at"]), "decided_at": _iso(a["decided_at"]),
                        "answers": [{"label": qq["label"], "answer": apps_lib.jl(a["answers"], {}).get(qq["id"])}
                                    for qq in (f["questions"] if f else [])] or
                                   [{"label": k, "answer": v} for k, v in apps_lib.jl(a["answers"], {}).items()]})
        return web.json_response(out)

    async def app_decide(request):
        m, adm, c = await p.session(request, perm="applications")
        b = await p.body(request)
        note = await apps_lib.decide(p.bot, p.guild, int(b["id"]), bool(b.get("accept")), m.id, (b.get("reason") or "").strip() or None)
        return web.json_response({"ok": True, "note": note})

    # ───────── staff access ─────────
    async def guild_meta(request):
        await p.session(request, perm="*")
        g = p.guild
        return web.json_response({
            "channels": [{"id": str(ch.id), "name": ch.name, "cat": ch.category.name if ch.category else ""} for ch in g.text_channels],
            "categories": [{"id": str(ch.id), "name": ch.name} for ch in g.categories],
            "roles": [{"id": str(r.id), "name": r.name, "color": str(r.color) if r.color.value else None}
                      for r in reversed(g.roles) if not r.is_default() and not r.managed]})

    async def staff_get(request):
        m, adm, c = await p.session(request, perm="staff")
        owners = sorted(int(x) for x in p.state.settings.get("owner_ids", []))
        ranks = c.get("staff_ranks") or []
        uids = set(owners) | {int(u) for rk in ranks for u in rk.get("users", []) if str(u).isdigit()}
        names = await p.whos(uids)
        g = p.guild
        def members_with(rk):   # how many people currently hold the rank through roles
            rids = {int(r) for r in rk.get("roles", []) if str(r).isdigit()}
            return len({mm.id for r in rids if g.get_role(r) for mm in g.get_role(r).members})
        return web.json_response({
            "owners": [names[str(o)] for o in owners], "is_owner": "owner" in adm,
            "ranks": [{**rk, "roles": [str(x) for x in rk.get("roles", [])], "users": [names.get(str(u)) or {"id": str(u), "name": str(u)} for u in rk.get("users", [])],
                       "role_members": members_with(rk)} for rk in ranks],
            "perms": {k: {"name": v[0], "desc": v[1]} for k, v in core.STAFF_PERMS.items()},
            "roles": [{"id": str(r.id), "name": r.name} for r in reversed(g.roles) if not r.is_default() and not r.managed],
            "bot_top": g.me.top_role.position})

    async def staff_save(request):
        import discord
        m, adm, c = await p.session(request, perm="staff")
        b = await p.body(request)
        msgs = []
        if "owners" in b:
            if "owner" not in adm:
                raise EconError("Only owners can change the owner list.")
            owners = sorted({int(x) for x in b["owners"] if str(x).strip().isdigit()})
            if not owners:
                raise EconError("There must be at least one owner.")
            if m.id not in owners and not b.get("confirm_self_remove"):
                raise EconError("You're removing yourself as owner — tick the confirm box if you really mean it.")
            p.state.settings["owner_ids"] = owners
            p.state.save()
            core.BOT_OWNERS.clear(); core.BOT_OWNERS.update(owners)
        if "ranks" in b:
            ranks = []
            for i, rk in enumerate((b["ranks"] or [])[:25]):
                name = str(rk.get("name") or "").strip()[:40]
                if not name:
                    continue
                ranks.append({"id": str(rk.get("id") or f"r{i}")[:24], "name": name,
                              "color": rk.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(rk.get("color") or "")) else "#38b6ff",
                              "perms": [x for x in rk.get("perms") or [] if x in core.STAFF_PERMS],
                              "roles": [int(x) for x in _ids(rk.get("roles"))], "users": [int(x) for x in _ids(rk.get("users"))],
                              "assign_role": bool(rk.get("assign_role"))})
            if "owner" not in adm:   # non-owners can't hand out 'staff' (would let them escalate)
                old = {r.get("id"): r for r in c.get("staff_ranks") or []}
                for rk in ranks:
                    if "staff" in rk["perms"] and "staff" not in (old.get(rk["id"], {}).get("perms") or []):
                        raise EconError("Only owners can give a rank the Staff management permission.")
            await set_cfg(p.gid, staff_ranks=ranks)
            # optionally give the rank's Discord role(s) to the users listed on it
            g = p.guild
            for rk in ranks:
                if not rk["assign_role"] or not rk["roles"]:
                    continue
                roles = [g.get_role(r) for r in rk["roles"] if g.get_role(r)]
                for uid in rk["users"]:
                    mem = await p.member(uid)
                    missing = [r for r in roles if mem and r not in mem.roles]
                    if missing:
                        try:
                            await mem.add_roles(*missing, reason=f"Staff rank {rk['name']} (web panel)")
                        except discord.Forbidden:
                            msgs.append(f"Couldn't give {', '.join(r.name for r in missing)} — move the bot's role above it.")
        await core.log(p.bot, p.gid, core.emb("🛡️ Staff access changed (web)", f"by <@{m.id}>", core.GOLD))
        return web.json_response({"ok": True, "warnings": sorted(set(msgs))})

    # ───────── routes ─────────
    r.add_get("/brand/logo", logo)
    r.add_get("/api/brand", brand_api)
    r.add_post("/api/brand/logo", upload_logo)
    r.add_get("/docs", docs_index)
    r.add_get("/docs/{slug}", doc_page)
    r.add_get("/api/docs", docs_list)
    r.add_get("/api/docs/{id:\\d+}", doc_get)
    r.add_post("/api/docs", doc_save)
    r.add_post("/api/docs/{id:\\d+}/delete", doc_delete)
    r.add_post("/api/docs/order", doc_order)
    r.add_post("/api/docs/import", doc_import)
    r.add_post("/api/panel/post", post_panel)
    r.add_get("/api/tickets", tickets_list)
    r.add_get("/api/tickets/{id:\\d+}", ticket_get)
    r.add_get("/api/guild-setup", guild_setup)
    r.add_post("/api/guild-setup", guild_setup_save)
    r.add_post("/api/guild-setup/autocreate", guild_autocreate)
    r.add_get("/api/guild-meta", guild_meta)
    r.add_get("/api/staff", staff_get)
    r.add_post("/api/staff", staff_save)
    r.add_get("/api/apply", forms_public)
    r.add_post("/api/apply", form_submit)
    r.add_get("/api/forms", forms_admin)
    r.add_post("/api/forms", form_save)
    r.add_post("/api/forms/{id:\\d+}/delete", form_delete)
    r.add_get("/api/applications", apps_list)
    r.add_post("/api/applications/decide", app_decide)
