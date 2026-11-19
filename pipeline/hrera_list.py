"""Every HRERA-registered project in a city, read from its public project page (form REP-I).

usage: python hrera_list.py <city>  -> out/<city>/hrera_projects.json

The authority and district come from data/cities.json ("hrera"). Values are kept as filed: land area in
acres, plot sizes and apartment carpet areas in square metres. Nothing is estimated. HRERA pages carry no
coordinates, so build_city.py places a project only through a matching Magicbricks project or a named OSM area.
Pages are fetched one per second and cached in cache/hrera.
"""
import json
import pathlib
import re
import sys

import hrera

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"


def page_lines(html):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    t = re.sub(r"<[^>]+>", "\n", t).replace("&nbsp;", " ").replace("&amp;", "&")
    return [l for l in (re.sub(r"\s+", " ", x).strip() for x in t.split("\n")) if l]


def _num(s):
    try:
        return float(re.sub(r"[^\d.]", "", s or "")) if re.search(r"\d", s or "") else None
    except ValueError:
        return None


def _after(L, label, skip_annex=True):
    for i, l in enumerate(L):
        if l.upper().startswith(label.upper()):
            for v in L[i + 1:i + 4]:
                if skip_annex and v.startswith("(Annex"):
                    continue
                return v
    return None


def _table(L, start, cols, stop):
    """Rows of `cols` values after the header line starting with `start`, up to a line starting with `stop`."""
    i = next((k for k, l in enumerate(L) if l.upper().startswith(start.upper())), None)
    if i is None:
        return []
    j = next((k for k in range(i + 1, len(L)) if re.match(stop, L[k], re.I)), len(L))
    cells = L[i + 1:j]
    while cells and not re.search(r"\d", cells[0]) and cells[0] != "Apartment/Shops/Other Buildings":
        cells = cells[1:]  # header cells
    return [cells[k:k + cols] for k in range(0, len(cells) - cols + 1, cols)]


def _ongoing_units(L):
    """Ongoing-project form: rows of serial, 'APARTMENT/SHOPS/OTHER BUILDINGS Type X', carpet sq m, count,
    total sq m, booked, unsold and (sometimes) towers."""
    i = next((k for k, l in enumerate(L) if l.startswith("No. of towers to be/ being constructed for booked apartments")), None)
    if i is None:
        return []
    rows, k = [], i + 1
    kind = "APARTMENT/SHOPS/OTHER BUILDINGS"
    while k + 3 < len(L) and re.fullmatch(r"\d+", L[k]) and L[k + 1].upper().startswith(kind):
        j = k + 2
        while j < len(L) and not (re.fullmatch(r"\d+", L[j]) and j + 1 < len(L) and L[j + 1].upper().startswith(kind)) and re.fullmatch(r"[\d.]+", L[j]):
            j += 1
        vals = L[k + 2:j]
        n = _num(vals[1]) if len(vals) > 1 else None
        if n:
            rows.append({"type": L[k + 1][len(kind):].strip(), "carpet_sqm": _num(vals[0]), "count": int(n),
                         "towers": int(_num(vals[5]) or 0) or None if len(vals) > 5 else None})
        k = j
    return rows


def parse_project(html):
    L = page_lines(html)
    plots = []
    for row in _table(L, "iii) Sizes of the plots", 3, r"Total$|iv\. Type of apartments"):
        sqm, n = _num(row[1]), _num(row[2])
        if sqm and n:
            plots.append({"plot_sqm": sqm, "count": int(n)})
    apts = []
    for row in _table(L, "iv. Type of apartments", 4, r"\(vi\)|\(v\)"):
        n = _num(row[2])
        if n:
            apts.append({"type": row[0], "carpet_sqm": _num(row[1]), "count": int(n), "towers": int(_num(row[3]) or 0) or None})
    apts = apts or _ongoing_units(L)
    return {
        "project_name": _after(L, "1. Name of the project"),
        "site_address": _after(L, "2. Address of the site of the project"),
        "tehsil": _after(L, "Tehsil", False),
        "land_acres": _num(_after(L, "1. Land area of the project")),
        "plot_acres": _num(_after(L, "PLOTS TO BE SOLD", False)),
        "apartment_land_acres": _num(_after(L, "LAND AREA TO BE USED FOR CONSTRUCTION OF APARTMENTS", False)),
        "plots": plots,
        "apartments": apts,
        "completion": _after(L, "ii) Likely date of completing the project", False),
    }


COMMERCIAL = re.compile(r"\b(MARKET|COMMERCIAL|MALL|SCO|SHOPS?|OFFICES?|UDYOG|INDUSTRIAL)\b", re.I)


def kind_of(p):
    """Project type from what the filing says it builds (shop-only or industrial filings are commercial)."""
    types = [a["type"] for a in p["apartments"]]
    if (types and all(re.search(r"SHOP|OFFICE|SCO|COMMERCIAL", t, re.I) for t in types)) or COMMERCIAL.search(p["name"]):
        return "commercial"
    if p["apartments"] or (p.get("apartment_land_acres") or 0) > 0:
        return "apartment"
    if p["plots"] or (p.get("plot_acres") or 0) > 0:
        return "plot"
    return "other"


def main(city):
    projects = hrera.registered_projects(city)
    out = []
    for i, p in enumerate(projects, 1):
        pid = p["url"].rstrip("/").rsplit("/", 1)[-1]
        try:
            d = parse_project(hrera.get(p["url"], f"preview_{pid}.html"))
        except Exception as e:  # keep the list row, say what failed
            d = {"error": str(e)[:120], "plots": [], "apartments": []}
        status = re.sub(r"^[A-Z0-9/-]+\s*", "", p["reg"]).strip() or None  # e.g. "Lapsed Project"
        rec = {**p, "reg": p["reg"].split()[0], "status": status, **d}
        rec["kind"] = kind_of(rec)
        out.append(rec)
        print(f"{i}/{len(projects)} {p['name'][:40]} -> {rec['kind']}", flush=True)
    (OUT / city).mkdir(parents=True, exist_ok=True)
    (OUT / city / "hrera_projects.json").write_text(json.dumps(out, indent=0))
    from collections import Counter
    print(len(out), "HRERA projects", Counter(r["kind"] for r in out))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "gurgaon")
