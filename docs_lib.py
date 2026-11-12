"""Document helpers: HTML sanitiser (for the doc builder) and Google Docs/Sheets importer."""
import csv, io, re, html
from html.parser import HTMLParser

import aiohttp

ALLOWED = {
    "h1", "h2", "h3", "h4", "p", "br", "hr", "strong", "b", "em", "i", "u", "s", "a", "ul", "ol", "li", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td", "img", "code", "pre", "div", "span", "mark", "sub", "sup",
}
VOID = {"br", "hr", "img"}
DROP_CONTENT = {"script", "style", "head", "title", "iframe", "object", "embed", "svg", "math", "noscript", "template"}
CLASS_OK = {"callout", "callout-info", "callout-warn", "callout-danger", "callout-ok", "pc-section", "toc", "lead"}


def _safe_url(u: str, img=False) -> str | None:
    u = (u or "").strip()
    if re.match(r"^https?://", u, re.I) or u.startswith("#") or (u.startswith("/") and not u.startswith("//")):
        return u
    if img and re.match(r"^data:image/(png|jpe?g|gif|webp);base64,", u, re.I):
        return u
    return None


class _Sanitiser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.drop = [], [], 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop += 1
            return
        if self.drop or tag not in ALLOWED:
            return
        a = dict(attrs)
        keep = []
        if tag == "a":
            u = _safe_url(a.get("href"))
            if u:
                keep.append(f'href="{html.escape(u)}"')
                if u.startswith("http"):
                    keep.append('target="_blank" rel="noopener noreferrer"')
        if tag == "img":
            u = _safe_url(a.get("src"), img=True)
            if not u:
                return
            keep.append(f'src="{html.escape(u)}"')
            keep.append(f'alt="{html.escape(a.get("alt") or "")}"')
            keep.append('loading="lazy"')
        if tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if str(a.get(k) or "").isdigit():
                    keep.append(f'{k}="{a[k]}"')
        if tag in ("h1", "h2", "h3", "h4") and re.fullmatch(r"[a-z0-9\-]{1,60}", a.get("id") or ""):
            keep.append(f'id="{a["id"]}"')
        cls = [c for c in (a.get("class") or "").split() if c in CLASS_OK]
        if cls:
            keep.append(f'class="{" ".join(cls)}"')
        self.out.append(f"<{tag}{(' ' + ' '.join(keep)) if keep else ''}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop or tag not in ALLOWED or tag in VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.drop:
            self.out.append(html.escape(data, quote=False))


def sanitise(raw: str) -> str:
    p = _Sanitiser()
    p.feed(raw or "")
    p.close()
    out = "".join(p.out) + "".join(f"</{t}>" for t in reversed(p.stack))
    out = re.sub(r"<(p|span|div)>\s*(?:&nbsp;| |\s)*</\1>", "", out)
    return out.strip()


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:60] or "doc"


def add_heading_ids(body: str) -> tuple[str, list[dict]]:
    """Give h1-h3 ids and return a table of contents."""
    toc, used = [], set()

    def rep(m):
        tag, inner = m.group(1), m.group(3)
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if not text:
            return m.group(0)
        base = slugify(html.unescape(text))[:50]
        sid, n = base, 2
        while sid in used:
            sid, n = f"{base}-{n}", n + 1
        used.add(sid)
        toc.append({"level": int(tag[1]), "id": sid, "text": html.unescape(text)})
        return f'<{tag} id="{sid}">{inner}</{tag}>'
    body = re.sub(r"<(h[1-3])(\s[^>]*)?>(.*?)</\1>", rep, body, flags=re.S)
    return body, toc


# ───────── Google import ─────────
def _doc_id(url: str, kind: str) -> str | None:
    m = re.search(rf"docs\.google\.com/{kind}/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _google_doc_to_html(raw: str) -> str:
    """Google's export styles everything with classes; turn the meaningful ones into real tags."""
    style = "".join(re.findall(r"<style[^>]*>(.*?)</style>", raw, re.S))
    bold, italic, under, sizes = set(), set(), set(), {}
    for cls, body in re.findall(r"\.(c\d+)\{([^}]*)\}", style):
        if "font-weight:700" in body:
            bold.add(cls)
        if "font-style:italic" in body:
            italic.add(cls)
        if "text-decoration:underline" in body:
            under.add(cls)
        m = re.search(r"font-size:(\d+(?:\.\d+)?)pt", body)
        if m:
            sizes[cls] = float(m.group(1))

    def span(m):
        classes = set(m.group(1).split())
        inner = m.group(2)
        if classes & under and "<a " not in inner:
            inner = f"<u>{inner}</u>"
        if classes & italic:
            inner = f"<em>{inner}</em>"
        if classes & bold:
            inner = f"<strong>{inner}</strong>"
        return inner
    raw = re.sub(r'<span class="([^"]*)">(.*?)</span>', span, raw, flags=re.S)

    def para(m):
        classes, inner = set(m.group(1).split()), m.group(2)
        text = re.sub(r"<[^>]+>", "", inner).replace("&nbsp;", " ").strip()
        size = max([sizes.get(c, 0) for c in classes] + [0])
        all_bold = inner.strip().startswith("<strong>") and inner.strip().endswith("</strong>")
        looks_like_sentence = text.startswith(("*", "http", "-", "•")) or len(text) > 70 or (text.count(" ") > 6 and not text.endswith(":"))
        if text and all_bold and not looks_like_sentence:
            plain = re.sub(r"</?strong>", "", inner)
            if size >= 18:
                return f"<h1>{plain}</h1>"
            if size >= 14:
                return f"<h2>{plain}</h2>"
            return f"<h3>{plain}</h3>"
        return f"<p>{inner}</p>"
    raw = re.sub(r'<p class="([^"]*)"[^>]*>(.*?)</p>', para, raw, flags=re.S)
    # Google wraps links in a redirector
    raw = re.sub(r'href="https://www\.google\.com/url\?q=([^&"]+)[^"]*"',
                 lambda m: f'href="{html.escape(__import__("urllib.parse").parse.unquote(m.group(1)))}"', raw)
    body = re.search(r"<body[^>]*>(.*)</body>", raw, re.S)
    return sanitise(body.group(1) if body else raw)


def _sheet_to_html(text: str) -> str:
    rows = [r for r in csv.reader(io.StringIO(text))]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return "<p>(empty sheet)</p>"
    width = max(len(r) for r in rows)
    used_cols = [i for i in range(width) if sum(1 for r in rows if i < len(r) and r[i].strip()) > max(2, len(rows) // 20)]
    rows = [[(r[i] if i < len(r) else "").strip() for i in used_cols] for r in rows]
    # header = first row with most filled cells near the top
    head_idx = max(range(min(10, len(rows))), key=lambda i: sum(1 for c in rows[i] if c))
    intro = [r for r in rows[:head_idx] if any(rows[:head_idx])]
    out = []
    for r in intro:
        line = " ".join(c for c in r if c)
        if line and len(line) > 3:
            out.append(f"<p class=\"lead\">{html.escape(line)}</p>")
    head = rows[head_idx]
    out.append("<table><thead><tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in head) + "</tr></thead><tbody>")
    for r in rows[head_idx + 1:]:
        filled = [c for c in r if c]
        if len(filled) == 1 and r[0]:
            out.append(f'<tr><td colspan="{len(head)}" class="pc-section"><strong>{html.escape(r[0])}</strong></td></tr>')
            continue
        out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return sanitise("".join(out))


async def import_google(url: str) -> tuple[str, str]:
    """Return (title, html) from a public Google Doc or Sheet link."""
    did = _doc_id(url, "document")
    sid = _doc_id(url, "spreadsheets")
    if not (did or sid):
        raise ValueError("That isn't a Google Docs or Google Sheets link.")
    export = (f"https://docs.google.com/document/d/{did}/export?format=html" if did else
              f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
        async with s.get(export, allow_redirects=True) as r:
            if r.status != 200 or "accounts.google.com" in str(r.url):
                raise ValueError("Google wouldn't share it — set the document to 'Anyone with the link can view'.")
            text = await r.text()
        title = ""
        async with s.get(url.split("?")[0].replace("/edit", "") + "/edit", allow_redirects=True) as r:
            if r.status == 200:
                m = re.search(r"<title>(.*?)</title>", await r.text(), re.S)
                if m:
                    title = re.sub(r"\s*-\s*Google (Docs|Sheets)\s*$", "", html.unescape(m.group(1))).strip()
    body = _google_doc_to_html(text) if did else _sheet_to_html(text)
    return title or "Imported document", body
