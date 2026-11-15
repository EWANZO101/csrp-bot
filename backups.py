"""Database backups: automatic daily dumps, manual "back up now", download, upload and restore — all from the web panel.

Files live in DATA_DIR/backups/db as gzip'd SQL dumps (`mariadb-dump --add-drop-table`), so a file can also be restored by
hand:  gunzip -c FILE.sql.gz | docker compose exec -T db sh -c 'mariadb -ueconomybot -p"$MARIADB_PASSWORD" economybot'
Uploads accept .sql, .sql.gz, or the csrp-backup.tar.gz made by backup.sh (only its backup.sql is used).
Restoring first takes an automatic "pre-restore" backup, then replaces every table in the dump and restarts the bot.
"""
import asyncio, datetime as dt, gzip, hashlib, json, logging, os, re, shutil, tarfile, tempfile

import aiohttp

from aiohttp import web

import core
from core import EconError

logger = logging.getLogger("economy.backups")
DIR = os.path.join(os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")), "backups", "db")
NAME_RE = re.compile(r"^csrp-db-\d{4}-\d{2}-\d{2}_\d{6}-(auto|manual|pre-restore|uploaded|hub)\.sql\.gz$")
KEEP = {"auto": 14, "manual": 20, "pre-restore": 10, "uploaded": 10, "hub": 10}
DAILY_HOUR_UTC = 4
MAX_UPLOAD = 1024 * 1024 * 1024   # 1 GB
LOCK = asyncio.Lock()


def _db_args():
    return ["-h", os.getenv("DB_HOST", "127.0.0.1"), "-P", str(os.getenv("DB_PORT", "3306")),
            "-u", os.getenv("DB_USER", "economybot"), "--skip-ssl"]


def _env():
    return {**os.environ, "MYSQL_PWD": os.getenv("DB_PASS", "")}


async def _new_name(kind):
    while True:   # names are per-second; never overwrite one made in the same second
        name = f"csrp-db-{dt.datetime.utcnow():%Y-%m-%d_%H%M%S}-{kind}.sql.gz"
        if not os.path.exists(os.path.join(DIR, name)):
            return name
        await asyncio.sleep(1)


def list_backups():
    os.makedirs(DIR, exist_ok=True)
    out = []
    for f in os.listdir(DIR):
        m = NAME_RE.match(f)
        if not m:
            continue
        st = os.stat(os.path.join(DIR, f))
        out.append({"name": f, "kind": m.group(1), "size": st.st_size,
                    "created": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def _prune():
    by = {}
    for b in list_backups():
        by.setdefault(b["kind"], []).append(b)
    for kind, rows in by.items():
        for b in rows[KEEP.get(kind, 10):]:
            try:
                os.remove(os.path.join(DIR, b["name"]))
            except OSError:
                pass


async def make_backup(kind="manual") -> dict:
    """Dump the whole database to DIR/<name>.sql.gz. Returns the backup's info."""
    async with LOCK:
        return await _make_backup(kind)


async def _make_backup(kind):
    os.makedirs(DIR, exist_ok=True)
    name = await _new_name(kind)
    path = os.path.join(DIR, name)
    tmp = path + ".part"
    proc = await asyncio.create_subprocess_exec(
        "mariadb-dump", *_db_args(), "--single-transaction", "--routines", "--triggers", "--events",
        "--add-drop-table", "--default-character-set=utf8mb4", os.getenv("DB_NAME", "economybot"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_env())
    try:
        with gzip.open(tmp, "wb", compresslevel=6) as gz:
            while chunk := await proc.stdout.read(1 << 20):
                gz.write(chunk)
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        if await proc.wait() != 0:
            raise EconError(f"Backup failed: {err[:300] or 'mariadb-dump error'}")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    os.chmod(path, 0o600)
    _prune()
    info = next((b for b in list_backups() if b["name"] == name), {"name": name})
    logger.info("backup created: %s (%s bytes)", name, info.get("size"))
    return info


def _path(name):
    if not NAME_RE.match(name or ""):
        raise EconError("Unknown backup.")
    p = os.path.join(DIR, name)
    if not os.path.exists(p):
        raise EconError("That backup no longer exists.")
    return p


def _check_sql(path):
    with gzip.open(path, "rb") as f:
        head = f.read(65536).decode("utf-8", errors="replace")
    if not re.search(r"(MariaDB dump|MySQL dump|CREATE TABLE|INSERT INTO)", head, re.I):
        raise EconError("That file doesn't look like a database dump.")


async def restore_backup(name) -> dict:
    """Replace the database with the backup's contents (after taking a pre-restore backup)."""
    async with LOCK:
        path = _path(name)
        _check_sql(path)
        safety = await _make_backup("pre-restore")
        proc = await asyncio.create_subprocess_exec(
            "mariadb", *_db_args(), "--default-character-set=utf8mb4", os.getenv("DB_NAME", "economybot"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=_env())
        try:
            with gzip.open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        if await proc.wait() != 0:
            raise EconError(f"Restore failed (your data before the restore is saved as {safety['name']}): {err[:300]}")
        core._cfg_cache.clear()
        logger.warning("database restored from %s (pre-restore backup: %s)", name, safety["name"])
        return safety


async def import_upload(field, data_out: dict | None = None) -> dict:
    """Save an uploaded .sql / .sql.gz / backup.sh .tar.gz as an 'uploaded' backup (not restored yet).
    If `data_out` is a dict and the upload is a csrp-backup.tar.gz, the archive's data/settings.json is put in
    data_out["settings"] (parsed) and data/brand/* files in data_out["brand"] ({filename: bytes})."""
    os.makedirs(DIR, exist_ok=True)
    fname = (field.filename or "").lower()
    tmpdir = tempfile.mkdtemp(dir=os.path.dirname(DIR))
    try:
        raw = os.path.join(tmpdir, "upload")
        size = 0
        with open(raw, "wb") as f:
            while chunk := await field.read_chunk(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise EconError("File is too large (max 1 GB).")
                f.write(chunk)
        with open(raw, "rb") as f:
            magic = f.read(2)
        is_gz = magic == b"\x1f\x8b"
        sql_gz = os.path.join(tmpdir, "out.sql.gz")
        if is_gz and (fname.endswith((".tar.gz", ".tgz")) or tarfile.is_tarfile(raw)):
            with tarfile.open(raw, "r:gz") as t:
                member = next((m for m in t.getmembers() if m.isfile() and os.path.basename(m.name) == "backup.sql"), None) \
                    or next((m for m in t.getmembers() if m.isfile() and m.name.endswith(".sql")), None)
                if not member:
                    raise EconError("No .sql file found inside that archive.")
                src = t.extractfile(member)
                with gzip.open(sql_gz, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                if data_out is not None:
                    _read_bundle_data(t, data_out)
        elif is_gz:
            shutil.move(raw, sql_gz)
        else:
            with open(raw, "rb") as src, gzip.open(sql_gz, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
        try:
            _check_sql(sql_gz)
        except (OSError, EOFError):
            raise EconError("Couldn't read that file — upload a .sql, .sql.gz or csrp-backup.tar.gz.")
        name = await _new_name("uploaded")
        shutil.move(sql_gz, os.path.join(DIR, name))
        os.chmod(os.path.join(DIR, name), 0o600)
        _prune()
        return next(b for b in list_backups() if b["name"] == name)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _read_bundle_data(t: tarfile.TarFile, out: dict):
    """Pull data/settings.json and data/brand/* out of a backup.sh archive (read in memory, never extracted to disk
    by path, so names like ../../x can't escape)."""
    for m in t.getmembers():
        if not m.isfile() or m.size > 20 * 1024 * 1024:
            continue
        parts = [p for p in m.name.replace("\\", "/").split("/") if p not in ("", ".")]
        if ".." in parts or len(parts) < 2 or parts[0] != "data":
            continue
        if parts[1:] == ["settings.json"]:
            try:
                s = json.loads(t.extractfile(m).read().decode("utf-8"))
                if isinstance(s, dict):
                    out["settings"] = s
            except (ValueError, UnicodeDecodeError):
                logger.warning("backup archive has an unreadable data/settings.json — skipped")
        elif len(parts) == 3 and parts[1] == "brand" and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", parts[2]):
            out.setdefault("brand", {})[parts[2]] = t.extractfile(m).read()


# ───────── daily schedule ─────────
async def daily_loop():
    await asyncio.sleep(60)
    while True:
        try:
            now = dt.datetime.utcnow()
            autos = [b for b in list_backups() if b["kind"] == "auto"]
            last = dt.datetime.fromisoformat(autos[0]["created"]).replace(tzinfo=None) if autos else None
            due_today = now.replace(hour=DAILY_HOUR_UTC, minute=0, second=0, microsecond=0)
            if now < due_today:
                due_today -= dt.timedelta(days=1)
            if last is None or last < due_today:
                await make_backup("auto")
        except Exception:
            logger.exception("daily backup failed")
        await asyncio.sleep(600)


# ───────── OpsLab Bot Hub: send every backup there, and run backup/restore requests made on the hub ─────────
SYNC_FILE = os.path.join(DIR, ".hub-synced.json")
HUB_POLL = 60


def hub_prefs(state) -> dict:
    hp = state.settings.setdefault("hub_backups", {})
    hp.setdefault("sync", True)
    hp.setdefault("allow_restore", True)
    return hp


def _synced() -> set:
    try:
        with open(SYNC_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _mark_synced(names: set):
    keep = {b["name"] for b in list_backups()}
    with open(SYNC_FILE, "w") as f:
        json.dump(sorted(names & keep), f)


async def _hub_upload(hub, b):
    import hub_client
    with open(os.path.join(DIR, b["name"]), "rb") as f:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as s:
            async with s.post(hub_client.HUB_URL + "/api/v1/backups/upload", data=f,
                              headers={**hub._headers(), "X-Backup-Name": b["name"], "X-Backup-Kind": b["kind"],
                                       "Content-Type": "application/gzip"}) as r:
                if r.status >= 400:
                    try:
                        msg = (await r.json()).get("error")
                    except Exception:
                        msg = None
                    raise EconError(msg or f"hub upload HTTP {r.status}")


DATA_ROOT = os.path.dirname(os.path.dirname(DIR))      # DATA_DIR
DATA_SKIP_DIRS = {"backups"}
DATA_SKIP_FILES = {"update-request.json", "update-status.json", ".restored"}
DATA_STATE = os.path.join(DIR, ".hub-data.json")


def _data_files():
    for root, dirs, files in os.walk(DATA_ROOT):
        rel_root = os.path.relpath(root, DATA_ROOT)
        if rel_root == ".":
            dirs[:] = [d for d in dirs if d not in DATA_SKIP_DIRS]
        for f in sorted(files):
            if f in DATA_SKIP_FILES or ".bak" in f or f.endswith((".part", ".tmp")):
                continue
            yield os.path.join(root, f), os.path.normpath(os.path.join("data", rel_root, f))


async def _hub_send_data(hub):
    """Send data/ (settings incl. the bot token, branding, images) so the hub can rebuild this bot on a new server."""
    import hub_client
    h = hashlib.sha256()
    files = sorted(_data_files(), key=lambda x: x[1])
    for path, arc in files:
        h.update(arc.encode())
        with open(path, "rb") as f:
            h.update(hashlib.sha256(f.read()).digest())
    digest = h.hexdigest()
    try:
        with open(DATA_STATE) as f:
            last = json.load(f)
    except Exception:
        last = {}
    if last.get("sha") == digest and last.get("at", 0) > dt.datetime.utcnow().timestamp() - 86400:
        return False
    fd, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=os.path.dirname(DIR))
    os.close(fd)
    try:
        with tarfile.open(tmp, "w:gz") as t:
            for path, arc in files:
                t.add(path, arc, recursive=False)
        with open(tmp, "rb") as f:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
                async with s.post(hub_client.HUB_URL + "/api/v1/backups/data", data=f,
                                  headers={**hub._headers(), "Content-Type": "application/gzip"}) as r:
                    if r.status >= 400:
                        raise EconError(f"hub data upload HTTP {r.status}")
    finally:
        os.remove(tmp)
    with open(DATA_STATE, "w") as f:
        json.dump({"sha": digest, "at": dt.datetime.utcnow().timestamp()}, f)
    return True


async def hub_sync_once(state):
    hub = getattr(state, "hub", None)
    if not hub or not (state.settings.get("hub") or {}).get("key") or not hub_prefs(state)["sync"]:
        return 0
    done = _synced()
    sent = 0
    for b in reversed(list_backups()):          # oldest first
        if b["kind"] == "hub" or b["name"] in done:
            continue
        await _hub_upload(hub, b)
        done.add(b["name"])
        _mark_synced(done)
        sent += 1
    try:
        await _hub_send_data(hub)
    except Exception as e:
        logger.warning("hub settings upload failed: %s", e)
    return sent


async def _hub_job(p, job):
    import hub_client
    hub = p.state.hub
    action = job.get("action")
    if action == "backup":
        b = await make_backup("manual")
        await hub_sync_once(p.state)
        return True, f"Backup {b['name']} made and sent", False
    if action != "restore":
        return False, f"Unknown job {action}", False
    if not hub_prefs(p.state)["allow_restore"]:
        return False, "Restoring from the hub is turned off in this bot's panel (Settings → Backups)", False
    meta = job.get("backup") or {}
    os.makedirs(DIR, exist_ok=True)
    name = await _new_name("hub")
    path = os.path.join(DIR, name)
    h = hashlib.sha256()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as s:
        async with s.get(f"{hub_client.HUB_URL}/api/v1/backups/{int(meta['id'])}/file", headers=hub._headers()) as r:
            if r.status != 200:
                return False, f"Couldn't download the backup from the hub (HTTP {r.status})", False
            with open(path + ".part", "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    h.update(chunk)
                    f.write(chunk)
    if meta.get("sha256") and h.hexdigest() != meta["sha256"]:
        os.remove(path + ".part")
        return False, "Download was corrupted (checksum mismatch) — try again", False
    os.replace(path + ".part", path)
    os.chmod(path, 0o600)
    try:
        safety = await restore_backup(name)
    except EconError as e:
        return False, str(e), False
    return True, f"Restored from {meta.get('name')} — previous data saved as {safety['name']}", True


async def hub_loop(p):
    import hub_client
    await asyncio.sleep(30)
    while True:
        try:
            st = p.state
            if getattr(st, "hub", None) and (st.settings.get("hub") or {}).get("key"):
                try:
                    await hub_sync_once(st)
                except Exception as e:
                    logger.warning("hub backup sync failed: %s", e)
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                    async with s.get(hub_client.HUB_URL + "/api/v1/backups/jobs", headers=st.hub._headers()) as r:
                        job = (await r.json()).get("job") if r.status == 200 else None
                if job:
                    logger.info("hub backup job %s: %s", job.get("id"), job.get("action"))
                    try:
                        ok, detail, restart = await _hub_job(p, job)
                    except Exception as e:
                        logger.exception("hub backup job failed")
                        ok, detail, restart = False, f"{type(e).__name__}: {e}"[:500], False
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                        await s.post(f"{hub_client.HUB_URL}/api/v1/backups/jobs/{int(job['id'])}/result",
                                     json={"ok": ok, "detail": detail}, headers=st.hub._headers())
                    if p.ready:
                        try:
                            title = ("♻️ Database restored from the hub" if restart else "💾 Hub backup request") if ok else "⚠️ Hub backup request failed"
                            await core.log(p.bot, p.gid, core.emb(title, detail, core.RED if restart or not ok else core.GOLD))
                        except Exception:
                            pass
                    if restart:
                        await asyncio.sleep(1)
                        asyncio.ensure_future(st.start_bot())
                    try:
                        await hub_sync_once(st)   # send the pre-restore backup straight away
                    except Exception:
                        pass
                    continue   # check for another job right away
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("hub backup poll failed: %s", e)
        await asyncio.sleep(HUB_POLL)


# ───────── web routes ─────────
def register(p):
    r = p.app.router

    async def _log(title, text, color=core.GOLD):
        if p.ready:
            try:
                await core.log(p.bot, p.gid, core.emb(title, text, color))
            except Exception:
                pass

    async def api_list(request):
        await p.session(request, perm="setup")
        synced = _synced()
        hub_on = bool((p.state.settings.get("hub") or {}).get("key"))
        return web.json_response({"backups": [{**b, "on_hub": b["name"] in synced} for b in list_backups()],
                                  "daily_hour_utc": DAILY_HOUR_UTC, "keep": KEEP, "hub": {"connected": hub_on, **hub_prefs(p.state)}})

    async def api_hub_prefs(request):
        m, _, _ = await p.session(request, perm="setup")
        body = await p.body(request)
        hp = hub_prefs(p.state)
        for k in ("sync", "allow_restore"):
            if k in body:
                hp[k] = bool(body[k])
        p.state.save()
        await _log("💾 Hub backup settings changed", f"sync={hp['sync']}, restore from hub={hp['allow_restore']} by <@{m.id}>")
        return web.json_response({"ok": True, **hp})

    async def api_create(request):
        m, _, _ = await p.session(request, perm="setup")
        b = await make_backup("manual")
        await _log("💾 Database backup created", f"`{b['name']}` by <@{m.id}>")
        return web.json_response({"ok": True, "backup": b})

    async def api_download(request):
        await p.session(request, perm="setup")
        name = request.match_info["name"]
        return web.FileResponse(_path(name), headers={
            "Content-Disposition": f'attachment; filename="{name}"', "Content-Type": "application/gzip",
            "Cache-Control": "no-store"})

    async def api_upload(request):
        m, _, _ = await p.session(request, perm="setup")
        reader = await request.multipart()
        field = await reader.next()
        if not field or field.name != "file":
            raise EconError("No file uploaded.")
        b = await import_upload(field)
        await _log("💾 Database backup uploaded", f"`{b['name']}` by <@{m.id}> (not restored yet)")
        return web.json_response({"ok": True, "backup": b})

    async def api_restore(request):
        m, _, _ = await p.session(request, perm="setup")
        body = await p.body(request)
        if (body.get("confirm") or "").strip().upper() != "RESTORE":
            raise EconError("Type RESTORE to confirm.")
        name = body.get("name") or ""
        safety = await restore_backup(name)
        await _log("♻️ Database restored", f"From `{name}` by <@{m.id}>. Previous data saved as `{safety['name']}`.", core.RED)
        asyncio.get_running_loop().call_later(1, lambda: asyncio.ensure_future(p.state.start_bot()))
        return web.json_response({"ok": True, "pre_restore": safety["name"]})

    async def api_delete(request):
        m, _, _ = await p.session(request, perm="setup")
        name = (await p.body(request)).get("name") or ""
        os.remove(_path(name))
        await _log("🗑️ Database backup deleted", f"`{name}` by <@{m.id}>")
        return web.json_response({"ok": True})

    r.add_get("/api/backups", api_list)
    r.add_post("/api/backups/create", api_create)
    r.add_get("/api/backups/download/{name}", api_download)
    r.add_post("/api/backups/upload", api_upload)
    r.add_post("/api/backups/restore", api_restore)
    r.add_post("/api/backups/delete", api_delete)
    r.add_post("/api/backups/hub", api_hub_prefs)

    async def _start(app):
        app["backup_task"] = asyncio.create_task(daily_loop())
        app["backup_hub_task"] = asyncio.create_task(hub_loop(p))

    async def _stop(app):
        for k in ("backup_task", "backup_hub_task"):
            if app.get(k):
                app[k].cancel()

    p.app.on_startup.append(_start)
    p.app.on_cleanup.append(_stop)
