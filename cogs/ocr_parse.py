"""Turn OCR text from a screenshot of an old-bot card back into a card the importer understands.

parse(text) -> dict | None:
  {"kind": "balance", "name": "e", "wallet": 0, "bank": 100000, "businesses": [(name, bal)], "departments": [...]}
  {"kind": "inventory", "name": "thebigchonch", "items": ["Lighthouse", ...], "page": 1}
  {"kind": "org_inventory", "org": "Black Eagle Gun Co.", "items": [(name, price, stock)]}
  {"kind": "org_list", "rows": [(name, owner_name, balance)]}
Only returns something when the card's structure is clearly recognised; anything else is left for review.
"""
import re

MONEY = r"-?\$\s?-?[\d,]+(?:\.\d+)?"
BULLET = re.compile(r"^\s*[•·*«»e©®\-–]\s+(.+?)\s*$")


def money(s):
    m = re.search(r"(-?)\$\s?(-?)([\d,]+)", s or "")
    if not m:
        return None
    n = int(m.group(3).replace(",", "") or 0)
    return -n if (m.group(1) or m.group(2)) else n


def lines_of(text):
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def name_after(pattern, ls):
    for l in ls:
        m = re.search(pattern, l, re.I)
        if m:
            return m.group(1).strip().lstrip("@").strip()
    return None


def org_lines(ls, start, stop_words):
    out = []
    for l in ls[start + 1:]:
        if any(w in l.lower() for w in stop_words):
            break
        m = re.match(rf"^\W*(.+?)\s*[-–—:]\s*({MONEY})\s*$", l)
        if m:
            out.append((m.group(1).strip(" *"), money(m.group(2))))
    return out


def parse(text):
    ls = lines_of(text)
    low = "\n".join(ls).lower()
    # ── 💰 Account Balance ──
    if "account balance" in low:
        name = name_after(r"balances for\s*@?\s*(.+)$", ls)
        wallet = bank = None
        for i, l in enumerate(ls):
            has_w, has_b = "wallet" in l.lower(), bool(re.search(r"\bbank\b", l, re.I))
            vals = re.findall(MONEY, l)
            nxt = re.findall(MONEY, ls[i + 1]) if i + 1 < len(ls) else []
            if has_w and has_b:
                v = vals if len(vals) >= 2 else nxt
                if len(v) >= 2:
                    wi, bi = l.lower().index("wallet"), re.search(r"\bbank\b", l, re.I).start()
                    first, second = money(v[0]), money(v[1])
                    wallet, bank = (first, second) if wi < bi else (second, first)
            elif has_w and wallet is None:
                v = vals or nxt
                wallet = money(v[0]) if v else None
            elif has_b and bank is None and "business" not in l.lower():
                v = vals or nxt
                bank = money(v[0]) if v else None
        if name and wallet is not None and bank is not None:
            biz = dept = []
            for i, l in enumerate(ls):
                if "businesses" in l.lower():
                    biz = org_lines(ls, i, ("department", "csrp economy", "today at"))
                if l.lower().endswith("departments") and "debug" not in l.lower():
                    dept = org_lines(ls, i, ("csrp economy", "today at", "business"))
            return {"kind": "balance", "name": name, "wallet": wallet, "bank": bank, "businesses": biz, "departments": dept}
        return None
    # ── 📦 Inventory — Business ──
    m = re.search(r"inventory\s*[—–\-]\s*(.+)$", "\n".join(ls), re.I | re.M)
    if m and "price" in low:
        rows = []
        for l in ls:
            r = re.match(rf"^\W*(.+?)\s*[—–\-]\s*price:?\s*`?({MONEY})`?\s*\|?\s*stock:?\s*`?([\d,]+|∞)`?", l, re.I)
            if r:
                st = r.group(3).replace(",", "")
                rows.append((r.group(1).strip(" *"), money(r.group(2)), int(st) if st.isdigit() else None))
        return {"kind": "org_inventory", "org": m.group(1).strip(" *"), "items": rows} if rows else None
    # ── 🎒 Inventory for player ──
    name = name_after(r"inventory\s+(?:for|of)\s+@?(.+)$", ls)
    if name:
        items = [BULLET.match(l).group(1) for l in ls if BULLET.match(l)]
        page = re.search(r"page\s*(\d+)\s*/\s*\d+", low)
        if items:
            return {"kind": "inventory", "name": name, "items": items, "page": int(page.group(1)) if page else 1}
        return None
    # ── 📋 Business List ──
    if "business list" in low:
        rows = []
        for l in ls:
            r = re.match(rf"^\W*(.+?)\s*[—–\-]\s*owned by\s*@?(.+?)\s*\|\s*balance:?\s*`?({MONEY})`?", l, re.I)
            if r:
                rows.append((r.group(1).strip(" *"), r.group(2).strip(), money(r.group(3))))
        return {"kind": "org_list", "rows": rows} if rows else None
    return None
