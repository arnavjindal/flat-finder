"""Merge Magicbricks projects, OSM shapes and verified research into the web data files.

usage: python build_city.py <city>  -> web/public/data/<city>.json, <city>-shapes.geojson, cities.json
"""
import json
import math
import pathlib
import re
import sys
from collections import defaultdict

import csv
import statistics
import urllib.parse

import geo
import mb_parse
import pricing
import rooms_model
import verified

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"
WEB = ROOT.parent / "web" / "public" / "data"
GENERIC = {"the", "of", "at", "in", "sector", "gurgaon", "gurugram", "hisar", "hissar", "residency", "residences", "homes",
           "apartments", "apartment", "towers", "tower", "heights", "city", "phase", "park", "estate",
           "floors", "society", "cghs", "group", "housing", "and", "by", "new", "greens", "green", "plaza"}
TOWER_H = 55.0  # visual default height (m) for generated apartment towers; shapes are not data


def tokens(name):
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in GENERIC and len(t) > 2}


UNIT_KIND = {"10002": "apartment", "10021": "apartment", "10003": "builder floor", "10000": "plot", "10017": "villa",
             "10007": "commercial", "10008": "commercial", "10009": "commercial", "10022": "apartment"}


def kind_of(p):
    t = (p.get("config_text") or "").lower()
    if not t:
        codes = [u["kind"] for u in (p.get("configs") or {}).get("units", []) or p.get("card_units") or []] + (p.get("type_codes") or [])
        kinds = [UNIT_KIND.get(c, "other") for c in codes]
        if kinds:
            for k in ("apartment", "builder floor", "villa", "plot", "commercial"):
                if k in kinds:
                    return k
        listed = [mb_parse.LISTING_KIND.get(l.get("type")) for l in (p.get("sale_listings") or []) + (p.get("rent_listings") or [])]
        listed = [k for k in listed if k]
        if listed:  # what the project's own listings are (Global Spaces Hisar: plots)
            return max(set(listed), key=listed.count)
        return infer_kind_from_name(p)
    if "apartment" in t or "penthouse" in t or "studio" in t or "flat" in t:
        return "apartment"
    if "floor" in t:
        return "builder floor"
    if "villa" in t or "house" in t:
        return "villa"
    if "plot" in t or "land" in t:
        return "plot"
    return "apartment" if re.search(r"\bbhk\b", t) else "other"


def infer_kind_from_name(p):
    """Last resort when the portal gives no type. Marked as inferred on the card."""
    n = " " + (p.get("name") or "").lower() + " "
    if re.search(r"\b(plots?|land)\b", n):
        return "plot~"
    if re.search(r"\b(villas?|kothi|bungalows?)\b", n):
        return "villa~"
    if re.search(r"\bfloors?\b", n):
        return "builder floor~"
    if re.search(r"\b(business|centre|center|mall|plaza|office|offices|commercial|corporate|hotel|hub|arcade|market|retail|square|district|spaces?)\b", n) and not p.get("units"):
        return "commercial~"
    if re.search(r"\b(business|corporate|mall|office|commercial|district)\b", n):
        return "commercial~"
    return "apartment~"


class Grid:
    """Bucket features by 0.01 degree cells for fast nearby lookups."""

    def __init__(self, items, key):
        self.cells = defaultdict(list)
        for it in items:
            x, y = key(it)
            self.cells[(int(x * 100), int(y * 100))].append(it)

    def near(self, pt, r=1):
        cx, cy = int(pt[0] * 100), int(pt[1] * 100)
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                yield from self.cells.get((cx + dx, cy + dy), [])


def match_area(project, area_grid):
    """OSM named area that contains the project point, or a name-matching one within 300 m."""
    pt = (project["lng"], project["lat"])
    pt_tokens = tokens(project["name"])
    best, best_score = None, 0
    for a in area_grid.near(pt):
        ring = a["geometry"]["coordinates"][0]
        inside = geo.point_in_ring(pt, ring)
        shared = len(pt_tokens & tokens(a["properties"]["name"]))
        if not inside and not shared:
            continue
        d = 0 if inside else geo.dist_m(pt, geo.centroid(ring))
        if d > 300:
            continue
        # skip neighbourhood-sized areas (whole sectors, DLF phases) unless the project itself is that big
        size = geo.area_m2(ring)
        limit = max(250_000, 3 * (project.get("acres") or 0) * 4047)
        if size > limit:
            continue
        score = shared * 10 + (5 if inside else 0) - d / 100
        if score > best_score:
            best, best_score = a, score
    return best


def dedupe(projects):
    """Merge listings with the same name tokens less than 400 m apart (phases, duplicates)."""
    kept = []
    for p in projects:
        for q in kept:
            if tokens(p["name"]) == tokens(q["name"]) and tokens(p["name"]) and \
                    geo.dist_m((p["lng"], p["lat"]), (q["lng"], q["lat"])) < 400:
                q.setdefault("merged", []).append(p["url"])
                break
        else:
            kept.append(p)
    return kept


def portal_units(p):
    units = []
    for u in (p.get("configs") or {}).get("units", []) or p.get("card_units") or []:
        units.append({"label": f"{u['bhk']} BHK {int(u['sqft']):,} sq ft" if u["bhk"] else f"{int(u['sqft']):,} sq ft",
                      "bhk": u["bhk"], "size_sqft": u["sqft"], "bedrooms": [], "rooms_complete": False,
                      "confidence": "portal", "source": p["url"], "page": None})
    return units


def norm(name):
    return " ".join(re.findall(r"[a-z0-9]+", (name or "").lower()))


def match_verified(v, projects, taken=()):
    """Portal project for a verified society: a match name must appear whole in the portal name, same sector if known."""
    best = None
    for p in projects:
        if p["mb_id"] in taken:
            continue
        if v.get("sector") and not re.search(rf"\b{v['sector']}\b", p.get("locality") or ""):
            continue
        n = f" {norm(p['name'])} "
        if any(f" {norm(m)} " in n for m in v["match"]):
            if best is None or len(p["name"]) < len(best["name"]):
                best = p
    return best


def events_for(s, city_name="Gurgaon"):
    ev = []
    sid = s["id"]
    src = s.get("url")
    if s.get("listing"):
        l = s["listing"]
        where = "exact pin" if l["location"] == "pin" else f"around the {s['locality'] or 'locality'} point · approximate (no exact pin on Magicbricks)"
        ev.append({"sid": sid, "step": "Located on map", "finding": f"{where} · {s['lat']:.4f}, {s['lng']:.4f}", "url": src})
        bits = [l["price_text"] and ("₹" + l["price_text"] + ("/mo" if l["deal"] == "rent" else "")), l["area"], l["posted"] and "posted " + l["posted"]]
        ev.append({"sid": sid, "step": f"Listed for {l['deal']} on Magicbricks", "finding": " · ".join(b for b in bits if b), "url": src})
        return ev
    if s.get("placed_by"):  # HRERA filing with no coordinates, placed at the OSM area of the same name
        ev.append({"sid": sid, "step": "Located on map", "finding": f"OSM area {s['placed_by']['name']} · {s['lat']:.4f}, {s['lng']:.4f}",
                   "url": "https://www.openstreetmap.org/" + s["placed_by"]["osm"]})
    else:
        ev.append({"sid": sid, "step": "Located on map", "finding": f"{s['locality'] or city_name} · {s['lat']:.4f}, {s['lng']:.4f}", "url": src})
    if s.get("hrera_url"):
        ev.append({"sid": sid, "step": "HRERA filing", "finding": s.get("rera_id") or "registered project", "url": s["hrera_url"]})
    bits = [f"{int(s['towers'])} towers" if s.get("towers") else None, f"{int(s['units']):,} units" if s.get("units") else None,
            f"{s['acres']} acres" if s.get("acres") else None]
    if any(bits):
        ev.append({"sid": sid, "step": "Project page read", "finding": " · ".join(b for b in bits if b), "url": src})
    if s.get("rera_id"):
        ev.append({"sid": sid, "step": "RERA registration", "finding": s["rera_id"][:60], "url": src})
    if s["amenities"]:
        ev.append({"sid": sid, "step": f"{len(s['amenities'])} amenities listed", "finding": ", ".join(s["amenities"][:4]), "url": src})
    for k, f in s.get("amen_facts", {}).items():
        ev.append({"sid": sid, "step": f"Checked {k} court", "finding": f"{f['value']} · p.{f.get('page')}", "url": f["source"], "verified": True})
    for u in s["unit_types"]:
        if u.get("ai"):
            a = u["ai"]
            bits = [f"narrowest bedroom {a['min_short']:.2f} ft" if a.get("min_short") else None,
                    f"every bedroom at least {a['long_lb']} ft long" if a.get("long_lb") else None,
                    f"and at least {a['area_lb']} sq ft" if a.get("area_lb") else None]
            ev.append({"sid": sid, "step": "Jev read floor plan", "finding": f"{u['bhk']} BHK · " + ", ".join(b for b in bits if b),
                       "url": u["source"], "verified": True, "unit": u["label"]})
            continue
        if u["bedrooms"] and u.get("confidence") == "ocr":
            rooms = ", ".join(f"{a:.1f} x {b:.1f} ft" for a, b in u["bedrooms"])
            ev.append({"sid": sid, "step": f"Read floor plan ({u.get('checked_by')})", "finding": f"{u['bhk']} BHK · bedrooms {rooms}",
                       "url": u["source"], "verified": True, "unit": u["label"]})
            continue
        if u["bedrooms"]:
            printed = ", ".join(u.get("bedrooms_printed") or [])
            ev.append({"sid": sid, "step": f"Reading plan p.{u['page']}", "finding": f"{u['label']} · bedrooms {printed}",
                       "url": u["source"], "verified": u["confidence"] == "primary", "unit": u["label"]})
        elif u.get("size_sqft"):
            ev.append({"sid": sid, "step": "Unit type listed", "finding": u["label"], "url": u["source"]})
    return ev


def shapes_for(s, area, bld_grid, sid_seed):
    feats = []
    is_tall = s["kind"] == "apartment"
    if area:
        ring = area["geometry"]["coordinates"][0]
        feats.append({"type": "Feature", "properties": {"sid": s["id"], "role": "site"}, "geometry": {"type": "Polygon", "coordinates": [ring]}})
        x0, y0, x1, y1 = geo.bbox(ring)
        inside = [b for b in bld_grid.near(geo.centroid(ring), r=2)
                  if x0 <= b["_c"][0] <= x1 and y0 <= b["_c"][1] <= y1 and geo.point_in_ring(b["_c"], ring)]
        if inside:
            for b in inside:
                h = b["properties"]["h"] or (TOWER_H if is_tall else 9.0)
                feats.append({"type": "Feature", "properties": {"sid": s["id"], "role": "bld", "h": h, "shape": "osm"}, "geometry": b["geometry"]})
            return feats, "osm"
        site, shape = ring, "osm-site"
    else:
        r = math.sqrt((s.get("acres") or 3) * 4047 / math.pi) if (s.get("acres") or 0) < 200 else 120
        site, shape = geo.circle((s["lng"], s["lat"]), max(40, min(r, 400))), "generated"
        feats.append({"type": "Feature", "properties": {"sid": s["id"], "role": "site"}, "geometry": {"type": "Polygon", "coordinates": [site]}})
    if s["kind"] == "plot":
        return feats, shape
    n = int(s.get("towers") or 0) or (max(2, min(12, round((s.get("units") or 300) / 120))) if is_tall else 8)
    w, d = (24, 18) if is_tall else (12, 10)
    for t in geo.generate_towers(site, n, seed=sid_seed, w_m=w, d_m=d):
        feats.append({"type": "Feature", "properties": {"sid": s["id"], "role": "bld", "h": TOWER_H if is_tall else 10.0, "shape": "generated"},
                      "geometry": {"type": "Polygon", "coordinates": [t]}})
    return feats, shape


SQM_TO_SQFT = 10.7639
PLACE_WORDS = {"enclave", "colony", "nagar", "vihar", "complex", "estates", "dham", "avenue", "royal", "urban", "vista", "model", "town"}


def sector_label(address):
    m = re.search(r"SECTOR[\s.-]*(\d+[A-Z]?)", (address or "").upper())
    return f"Sector {m.group(1)}" if m else None


def filed_units(h):
    """Unit rows exactly as filed with HRERA: apartment carpet areas and plot sizes (square metres)."""
    units = []
    for a in h.get("apartments") or []:
        m = re.search(r"(\d)\s*BHK", a["type"] or "", re.I)
        units.append({"label": f"{a['type']} · {a['count']} filed" + (f" · {a['carpet_sqm']:g} sq m carpet" if a.get("carpet_sqm") else ""),
                      "bhk": int(m.group(1)) if m else None, "carpet_sqft": round(a["carpet_sqm"] * SQM_TO_SQFT) if a.get("carpet_sqm") else None,
                      "bedrooms": [], "rooms_complete": False, "confidence": "filing", "source": h["url"], "page": None})
    for p in h.get("plots") or []:
        units.append({"label": f"Plot {p['plot_sqm']:g} sq m · {p['count']} filed", "bhk": None, "bedrooms": [], "rooms_complete": False,
                      "confidence": "filing", "source": h["url"], "page": None})
    return units


def add_hrera(city, cfg, projects, areas, in_box):
    """Attach each HRERA filing to its Magicbricks project. A filing with no portal project is added on its own
    only when a named OSM area of the same name places it (HRERA pages carry no coordinates).
    Returns (attached, added, not placed)."""
    f = OUT / city / "hrera_projects.json"
    if not f.exists():
        return 0, [], 0
    import hrera  # imported here: hrera imports tokens() from this module
    filed = json.loads(f.read_text())
    attached = 0
    for p in projects:
        h = hrera.match(p, [x for x in filed if not x.get("_mb")])
        if h:
            h["_mb"] = p["mb_id"]
            p["hrera_url"] = h["url"]
            p["rera_id"] = p.get("rera_id") or h["rera"]
            p["extra_units"] = filed_units(h)
            attached += 1
    added, unplaced = [], 0
    for h in filed:
        if h.get("_mb"):
            continue
        want = tokens(h["name"])  # the whole name must match: "Green Enclave" is not "Sainik Adarsh Enclave"
        area = next((a for a in areas if want - PLACE_WORDS and want == tokens(a["properties"]["name"])
                     and in_box(dict(zip(("lng", "lat"), geo.centroid(a["geometry"]["coordinates"][0]))))), None)
        if not area:
            unplaced += 1
            continue
        lng, lat = geo.centroid(area["geometry"]["coordinates"][0])
        completion = re.search(r"(\d{4})$", h.get("completion") or "")
        added.append({"mb_id": "h" + h["url"].rstrip("/").rsplit("/", 1)[-1], "name": h["name"].title(), "developer": (h["promoter"] or "").title() or None,
                      "locality": sector_label(h.get("site_address") or h["address"]) or (h.get("site_address") or h["address"]).title(),
                      "lat": lat, "lng": lng, "status": h.get("status") or "Registered with HRERA", "possession": completion.group(1) if completion else None,
                      "rera_id": h["rera"], "units": sum(x["count"] for x in h["apartments"] or h["plots"]) or None,
                      "towers": sum(x["towers"] or 0 for x in h["apartments"]) or None, "acres": h.get("land_acres"),
                      "kind_fixed": h["kind"], "extra_units": filed_units(h), "url": h["url"], "hrera_url": h["url"],
                      "placed_by": {"name": area["properties"]["name"], "osm": area["properties"]["osm"]}})
    return attached, added, unplaced


LISTING_LABEL = {"house": "house", "villa": "villa", "builder floor": "builder floor", "apartment": "flat", "plot": "plot", "farmland": "farmland"}


def add_listings(city, in_box):
    """Individual Magicbricks listings not in any project (houses, floors, flats, plots, farmland; sale and rent).
    Placed at the listing's own pin when it is a real one. Otherwise around its locality's point, spread on a small
    spiral so each can be clicked, and marked approximate. A point Magicbricks gives to three or more different
    localities is its city-wide fallback and places nothing. Returns (records, not placed, on an exact pin)."""
    f = OUT / city / "mb_listings.json"
    if not f.exists():
        return [], 0, 0
    recs = sorted((r for r in json.loads(f.read_text()) if not r["in_project"]), key=lambda r: r["id"])
    names = defaultdict(set)
    for r in recs:
        for pt in (r["locality_point"], r["pin"]):
            if pt:
                names[tuple(pt)].add(r["locality"])
    fallback = [pt for pt, n in names.items() if len(n) > 2]  # neighbouring sectors may share one point; 3+ is the city default

    def real(pt):
        return bool(pt) and all(geo.dist_m(pt[::-1], q[::-1]) > 60 for q in fallback)
    spiral, out, unplaced, pinned = defaultdict(int), [], 0, 0
    for r in recs:
        lp = r["locality_point"] if real(r["locality_point"]) else None
        if real(r["pin"]) and not (lp and geo.dist_m(r["pin"][::-1], lp[::-1]) < 30):
            (lat, lng), how = r["pin"], "pin"
        elif lp:
            k = spiral[tuple(lp)]
            spiral[tuple(lp)] += 1
            d, a = 30 + 14 * math.sqrt(k), k * 2.39996  # metres from the locality point, golden-angle steps
            lat = lp[0] + d * math.sin(a) / geo.M_PER_DEG_LAT
            lng = lp[1] + d * math.cos(a) / geo.m_per_deg_lng(lp[0])
            how = "locality"
        else:
            unplaced += 1
            continue
        if not in_box({"lat": lat, "lng": lng}):
            unplaced += 1
            continue
        pinned += how == "pin"
        label = LISTING_LABEL[r["kind"]]
        area = f"{r['area']:g} {r['area_unit'].lower()}" if r.get("area") and r.get("area_unit") else None
        head = f"{r['bhk']} BHK {label}" if r["bhk"] else label.capitalize() + (f" {area}" if area else "")
        rent = r["deal"] == "rent"
        price = r["price"]
        sale_l = [{"bhk": r["bhk"], "price": price, "psf": round(price / r["built_sqft"]) if price and r.get("built_sqft") and not rent else None}] if r["bhk"] and price else []
        out.append({"mb_id": "l" + r["id"], "name": head + (" for rent" if rent else "") + (f", {r['locality']}" if r["locality"] else ""),
                    "developer": f"{r['seller']} listing" if r.get("seller") else "Listing", "locality": r["locality"], "lat": lat, "lng": lng,
                    "status": r.get("status"), "price_min": None if rent else price, "price_max": None if rent else price,
                    "kind_fixed": r["kind"], "config_text": r["type"] + (" for rent" if rent else " for sale"), "url": r["url"],
                    "extra_units": [{"label": head + (f" · {area}" if area and r["bhk"] else ""), "bhk": r["bhk"], "size_sqft": r.get("built_sqft"),
                                     "bedrooms": [], "rooms_complete": False, "confidence": "portal", "source": r["url"], "page": None}],
                    "images": [{"thumb": r["image"], "full": r["image"]}] if r.get("image") else [],
                    "rent_listings" if rent else "sale_listings": sale_l, "rent_count" if rent else "sale_count": 1,
                    "listing": {"deal": r["deal"], "price_text": r.get("price_text"), "area": area, "carpet": r.get("carpet"), "posted": r.get("posted"),
                                "seller": r.get("seller"), "floor": r.get("floor"), "furnished": r.get("furnished"), "location": how}})
    return out, unplaced, pinned


def listing_shapes(s):
    """A small footprint for one listing: farmland and plots as an outline, homes with one low building."""
    r = {"farmland": 30, "plot": 14}.get(s["kind"], 12)
    feats = [{"type": "Feature", "properties": {"sid": s["id"], "role": "site"}, "geometry": {"type": "Polygon", "coordinates": [geo.circle((s["lng"], s["lat"]), r, n=16)]}}]
    if s["kind"] not in ("plot", "farmland"):
        h = 14.0 if s["kind"] in ("apartment", "builder floor") else 8.0
        feats.append({"type": "Feature", "properties": {"sid": s["id"], "role": "bld", "h": h, "shape": "generated"},
                      "geometry": {"type": "Polygon", "coordinates": [geo.rect((s["lng"], s["lat"]), 10, 8, 0)]}})
    return feats


def main(city="gurgaon"):
    cfg = json.loads((ROOT.parent / "data" / "cities.json").read_text())[city]
    out = OUT / city
    raw = json.loads((out / "mb_projects.json").read_text()) if (out / "mb_projects.json").exists() else []
    s0, w0, n0, e0 = cfg["bbox"]
    s0, w0, n0, e0 = cfg.get("located_box") or (s0 - 0.05, w0 - 0.05, n0 + 0.05, e0 + 0.05)

    def in_box(p):
        return s0 < p["lat"] < n0 and w0 < p["lng"] < e0
    located = [p for p in raw if p.get("lat") and p.get("lng") and in_box(p)]
    not_located = len(raw) - len(located)
    projects = dedupe(located)
    areas = json.loads((out / "osm_areas.geojson").read_text())["features"]
    hrera_attached, hrera_added, hrera_unplaced = add_hrera(city, cfg, projects, areas, in_box)
    projects += hrera_added
    not_located += hrera_unplaced
    listings, listings_unplaced, listings_pinned = add_listings(city, in_box)
    projects += listings  # after dedupe: listings with similar titles are different homes
    not_located += listings_unplaced
    area_grid = Grid(areas, lambda a: geo.centroid(a["geometry"]["coordinates"][0]))
    blds = json.loads((out / "osm_buildings.geojson").read_text())["features"]
    for b in blds:
        b["_c"] = geo.centroid(b["geometry"]["coordinates"][0])
    bld_grid = Grid(blds, lambda b: b["_c"])

    vfile = verified.ROOT / "data" / "verified" / f"{city}.json"
    ver = verified.load_verified(vfile) if vfile.exists() else []
    societies, shapes, events, used_areas = [], [], [], set()
    for i, p in enumerate(projects):
        s = {"id": f"s{p['mb_id']}", "name": p["name"], "developer": p.get("developer"), "locality": p.get("locality"),
             "lat": round(p["lat"], 6), "lng": round(p["lng"], 6), "status": p.get("status"), "possession": p.get("possession"),
             "rera_id": p.get("rera_id"), "units": p.get("units"), "towers": p.get("towers"), "acres": p.get("acres"),
             "price_min": p.get("price_min"), "price_max": p.get("price_max"), "kind": kind_of(p).rstrip("~"),
             "kind_inferred": kind_of(p).endswith("~"),
             "config_text": p.get("config_text"), "amenities": p.get("amenities") or [], "amen_facts": {},
             "unit_types": portal_units(p), "url": p["url"], "brochure_url": p.get("brochure_url"),
             "plan_links": {b: v["plan_images"] for b, v in ((p.get("configs") or {}).get("by_bhk") or {}).items() if v["plan_images"]},
             "verified": False, "note": None, "images": p.get("images") or [],
             "rating": p.get("rating"), "rating_count": p.get("rating_count"), "review_count": p.get("review_count"),
             "ratings": p.get("ratings") or {}, "reviews": p.get("reviews") or [], "locality_rating": p.get("locality_rating"),
             "sale_count": p.get("sale_count"), "rent_count": p.get("rent_count"), "psf_now": p.get("psf_now"),
             "trend": p.get("trend") or [], "description": p.get("description"),
             "gmaps": "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(f"{p['name']}, {p.get('locality') or ''}, {cfg['name']}"),
             "prices": pricing.listing_stats(p)}
        if s["towers"] and s["towers"] > 60:  # portal typo (e.g. 1500 towers): not plausible, drop it
            s["towers"] = None
        if p.get("kind_fixed"):  # HRERA-only project or single listing: type as filed or listed
            s["kind"], s["kind_inferred"] = p["kind_fixed"], False
        s["unit_types"] += p.get("extra_units") or []
        for k in ("hrera_url", "placed_by", "listing"):
            if p.get(k):
                s[k] = p[k]
        p["_s"] = s
        societies.append(s)

    taken = set()
    for v in ver:
        p = match_verified(v, projects, taken)
        if not p:
            print("verified society not found in portal data:", v["name"])
            continue
        taken.add(p["mb_id"])
        s = p["_s"]
        s["verified"] = bool(v["unit_types"])
        s["note"] = v["note"]
        s["amen_facts"] = v["amen_facts"]
        if v["unit_types"]:
            s["unit_types"] = v["unit_types"] + [u for u in s["unit_types"] if u["bhk"] not in {x["bhk"] for x in v["unit_types"]}]

    # bedroom sizes read from floor plan images: local OCR first (every printed size), Jev as a fallback and cross-check
    vdir = verified.ROOT / "data" / "verified"
    ai_plans = json.loads((vdir / f"{city}-ai.json").read_text())["plans"] if (vdir / f"{city}-ai.json").exists() else {}
    ocr_plans = json.loads((vdir / f"{city}-ocr.json").read_text())["plans"] if (vdir / f"{city}-ocr.json").exists() else {}
    by_sid = {s["id"]: s for s in societies}
    for url in set(ai_plans) | set(ocr_plans):
        o, j = ocr_plans.get(url, {}), ai_plans.get(url, {})
        s = by_sid.get(o.get("sid") or j.get("sid"))
        if not s or s.get("verified"):
            continue
        bhk = j.get("bhk") or o.get("bhk_listed") or j.get("bhk_listed")
        sizes = [u.get("size_sqft") or u.get("super_sqft") for u in s["unit_types"] if u.get("bhk") == bhk and (u.get("size_sqft") or u.get("super_sqft"))]
        unit = {"label": f"{bhk} BHK floor plan", "bhk": bhk, "size_range": [min(sizes), max(sizes)] if sizes else None,
                "source": url, "page": "plan image"}
        beds = [b["ft"] for b in o.get("bedrooms") or []]
        if o.get("status") == "read":
            unit.update(bedrooms=[list(b) for b in beds], rooms_complete=True, confidence="ocr", checked_by=o.get("checked_by"))
        elif beds and o.get("status") != "OCR and Jev disagree":
            unit.update(bedrooms=[list(b) for b in beds], rooms_complete=False, confidence="ocr", checked_by="OCR, some bedrooms")
        elif j.get("status") == "read" and o.get("status") != "OCR and Jev disagree":
            ai = {k: j.get(k) for k in ("min_short", "long_lb", "long_ub", "area_lb", "area_ub")}
            if ai.get("area_lb"):
                ai["area_lb"] -= 2  # spot checks found a 119.9 sq ft room passed as "at least 120": keep a small margin
            unit.update(bedrooms=[], rooms_complete=False, ai=ai, confidence="ai", checked_by="Jev only")
        else:
            continue
        s["unit_types"].append(unit)
        s["ai_read"] = True

    # bedroom sizes OCR'd from builder brochure pages. A page can hold two unit plans side by side, so a
    # brochure reading is never "complete": a failing room still fails, passing rooms only make it "likely".
    br_file = vdir / f"{city}-brochures.json"
    brochures = json.loads(br_file.read_text())["brochures"] if br_file.exists() else {}
    for sid, b in brochures.items():
        s = by_sid.get(sid)
        if not s or s.get("verified") or b.get("status") != "read":
            continue
        seen = set()
        for pg in b["pages"]:
            beds = sorted(tuple(x["ft"]) for x in pg["bedrooms"])
            if not beds or tuple(beds) in seen:
                continue
            seen.add(tuple(beds))
            s["unit_types"].append({"label": f"{pg['bhk']} BHK plan" if pg.get("bhk") else "Unit plan", "bhk": pg.get("bhk"),
                                    "bedrooms": [list(x) for x in beds], "rooms_complete": False, "confidence": "ocr",
                                    "checked_by": "OCR of the brochure", "source": b["url"], "page": str(pg["page"])})
            s["ai_read"] = True

    # bedroom sizes OCR'd from HRERA filings (approved building plans, floor plan drawings); same caution as brochures
    hr_file = vdir / f"{city}-hrera.json"
    hrera = json.loads(hr_file.read_text())["societies"] if hr_file.exists() else {}
    for sid, h in hrera.items():
        s = by_sid.get(sid)
        if not s or s.get("verified"):
            continue
        seen = set()
        for doc in h.get("read", []):
            for pg in doc.get("pages", []):
                beds = sorted(tuple(x["ft"]) for x in pg["bedrooms"])
                if not beds or tuple(beds) in seen:
                    continue
                seen.add(tuple(beds))
                s["unit_types"].append({"label": f"{pg['bhk']} BHK plan" if pg.get("bhk") else "Unit plan", "bhk": pg.get("bhk"),
                                        "bedrooms": [list(x) for x in beds], "rooms_complete": False, "confidence": "ocr",
                                        "checked_by": f"OCR of HRERA filing: {doc['doc'].title()[:60]}", "source": h["project"], "page": str(pg["page"])})
                s["ai_read"] = True

    # floor plans found on other sites by the plan finder, then OCR'd (found_ocr.py)
    fd_file = vdir / f"{city}-found.json"
    found = json.loads(fd_file.read_text())["societies"] if fd_file.exists() else {}
    for sid, reads in found.items():
        s = by_sid.get(sid)
        if not s or s.get("verified"):
            continue
        for r in reads:
            pages = r.get("pages") or ([{"page": None, "bhk": r.get("bhk"), "bedrooms": r.get("bedrooms") or []}] if r.get("bedrooms") else [])
            for pg in pages:
                beds = sorted(tuple(x["ft"]) for x in pg["bedrooms"])
                if not beds:
                    continue
                s["unit_types"].append({"label": f"{pg['bhk']} BHK plan" if pg.get("bhk") else "Unit plan", "bhk": pg.get("bhk"),
                                        "bedrooms": [list(x) for x in beds], "rooms_complete": r.get("status") == "read" and r["kind"] == "image",
                                        "confidence": "ocr", "checked_by": "OCR of a plan found online", "source": r.get("found_on") or r["url"],
                                        "page": str(pg["page"]) if pg.get("page") else "plan image"})
                s["ai_read"] = True

    # smallest-bedroom lower bounds from unit size, for units whose plans are unread
    room_model = rooms_model.fit(ver)
    for s in societies:
        for u in s["unit_types"]:
            if not u.get("bedrooms"):
                lb = rooms_model.lower_bounds(room_model, u.get("super_sqft") or u.get("size_sqft"), u.get("bhk"))
                if lb:
                    u["est_short"], u["est_area"] = lb

    # nearby places from OpenStreetMap
    pois_file = out / "osm_pois.json"
    pois = json.loads(pois_file.read_text()) if pois_file.exists() else []
    poi_grid = Grid(pois, lambda q: (q["lng"], q["lat"]))
    for s in societies:
        here = (s["lng"], s["lat"])
        near = {}
        for q in poi_grid.near(here, r=3):
            d = geo.dist_m(here, (q["lng"], q["lat"]))
            if d > 3000:
                continue
            k = q["k"]
            if k not in near or d < near[k]["m"]:
                near[k] = {"m": round(d, -1), "name": q["name"], "n": near.get(k, {}).get("n", 0)}
            if d <= 1000:
                near[k]["n"] = near[k].get("n", 0) + 1
        s["nearby"] = near

    # Flat Finder score (0-10): residents' rating, what is nearby, amenities. Shown with its parts.
    for s in societies:
        parts = {}
        if s.get("rating"):
            parts["residents"] = round(s["rating"] * 2, 1)
        nb = s["nearby"]
        reach = [nb.get("park", {}).get("m", 9e9) <= 500, nb.get("metro", {}).get("m", 9e9) <= 1500 or nb.get("rail", {}).get("m", 9e9) <= 1500,
                 nb.get("school", {}).get("m", 9e9) <= 1000, nb.get("hospital", {}).get("m", 9e9) <= 2000, nb.get("shopping", {}).get("m", 9e9) <= 1500]
        parts["nearby"] = round(10 * sum(reach) / len(reach), 1)
        parts["amenities"] = round(min(10, len(s["amenities"]) / 3), 1)
        w = {"residents": 0.5, "nearby": 0.3, "amenities": 0.2}
        tot = sum(w[k] for k in parts)
        s["score"] = round(sum(parts[k] * w[k] for k in parts) / tot, 1)
        s["score_parts"] = parts

    # prices: real listing medians first, then labelled estimates
    model = pricing.fit_yields(societies)
    print("price cells flagged as likely typos:", pricing.flag_outliers(societies))
    loc_psf = defaultdict(list)
    for s in societies:
        for b in s["prices"].values():
            if b.get("psf") and "buy" not in b.get("flags", {}):
                loc_psf[s["locality"]].append(b["psf"])
    for s in societies:
        sizes = defaultdict(list)
        for u in s["unit_types"]:
            size = u.get("super_sqft") or u.get("size_sqft")
            if u.get("bhk") and size:
                sizes[u["bhk"]].append(size)
        own = [b["psf"] for b in s["prices"].values() if b.get("psf") and "buy" not in b.get("flags", {})]
        psf = statistics.median(own) if own else s.get("psf_now") or (statistics.median(loc_psf[s["locality"]]) if len(loc_psf[s["locality"]]) >= 3 else None)
        if s["kind"] in ("apartment", "builder floor", "villa"):
            pricing.add_estimates(s, model, {b: statistics.median(v) for b, v in sizes.items()}, psf)
        s["prices"] = {str(b): v for b, v in sorted(s["prices"].items())}

    for i, s in enumerate(societies):
        if s.get("listing"):  # a single home: never claims an OSM area outline
            shapes.extend(listing_shapes(s))
            s["shape"] = "listing"
            events.extend(events_for(s, cfg["name"]))
            continue
        area = match_area(s, area_grid)
        if area and area["properties"]["osm"] in used_areas:
            area = None  # another society already owns this boundary
        if area:
            used_areas.add(area["properties"]["osm"])
            s["osm_area"] = area["properties"]["osm"]
        f, shape = shapes_for(s, area, bld_grid, i)
        s["shape"] = shape
        shapes.extend(f)
        events.extend(events_for(s, cfg["name"]))

    WEB.mkdir(parents=True, exist_ok=True)
    doc = {"city": {"id": city, "name": cfg["name"], "center": cfg["center"], "zoom": cfg["zoom"]},
            "price_model": model,
            "room_model": room_model,
            "counts": {"listed": len(raw) + len(hrera_added) + hrera_unplaced + len(listings) + listings_unplaced, "on_map": len(societies),
                       "not_located": not_located, "listings": len(listings), "listings_exact_pin": listings_pinned,
                       "listings_not_placed": listings_unplaced,
                       "ai_read": sum(1 for s in societies if s.get("ai_read")),
                       "for_sale": sum(s["sale_count"] or 0 for s in societies), "for_rent": sum(s["rent_count"] or 0 for s in societies),
                       "verified": sum(s["verified"] for s in societies), "hrera_attached": hrera_attached,
                       "hrera_only": len(hrera_added), "hrera_not_placed": hrera_unplaced},
            "sources": ["Magicbricks project pages", "OpenStreetMap contributors"]
            + ([f"HRERA {({1: 'Panchkula', 2: 'Gurugram'})[cfg['hrera']['authority']]}"] if cfg.get("hrera") else [])
            + ["Builder brochures (verified rows)"],
            "societies": societies, "events": events}
    details = {}
    for s in societies:  # heavy per-society text, loaded only when a card opens
        details[s["id"]] = {k: s.pop(k) for k in ("reviews", "description", "trend", "ratings", "images")}
    (WEB / f"{city}.json").write_text(json.dumps(doc, separators=(",", ":")))
    (WEB / f"{city}-details.json").write_text(json.dumps(details, separators=(",", ":")))
    for s in societies:
        s.update(details[s["id"]])
    write_csv(WEB / f"{city}-societies.csv", WEB / f"{city}-units.csv", societies)
    for f in shapes:
        f["geometry"]["coordinates"] = [[[round(x, 5), round(y, 5)] for x, y in r] for r in f["geometry"]["coordinates"]]
        if "h" in f["properties"]:
            f["properties"]["h"] = round(f["properties"]["h"])
    (WEB / f"{city}-shapes.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": shapes}, separators=(",", ":")))
    from collections import Counter
    built = sorted(p.stem for p in WEB.glob("*.json") if p.stem != "cities")
    all_cfg = json.loads((ROOT.parent / "data" / "cities.json").read_text())
    (WEB / "cities.json").write_text(json.dumps({c: {"name": all_cfg[c]["name"]} for c in built if c in all_cfg}))
    print(doc["counts"], Counter(s["shape"] for s in societies), Counter(s["kind"] for s in societies), "features", len(shapes))


def write_csv(soc_path, unit_path, societies):
    """Flat exports: one row per society, and one row per unit type with bedroom sizes."""
    cols = ["id", "name", "developer", "locality", "type", "lat", "lng", "status", "possession", "rera_id", "towers", "units", "acres",
            "listings_for_sale", "listings_for_rent", "price_range_min", "price_range_max", "price_per_sqft"]
    for b in range(1, 6):
        cols += [f"buy_{b}bhk_median", f"buy_{b}bhk_estimate", f"rent_{b}bhk_median", f"rent_{b}bhk_estimate", f"{b}bhk_price_flag"]
    cols += ["rating", "rating_count", "review_count", "amenities", "verified", "smallest_bedroom_ft", "magicbricks_url", "google_maps_url"]
    with open(soc_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in societies:
            rooms = [r for u in s["unit_types"] for r in (u.get("bedrooms") or [])]
            smallest = min(rooms, key=lambda r: r[0]) if rooms else None
            row = [s["id"], s["name"], s["developer"], s["locality"], s["kind"], s["lat"], s["lng"], s["status"], s["possession"], s["rera_id"],
                   s["towers"], s["units"], s["acres"], s["sale_count"], s["rent_count"], s["price_min"], s["price_max"], s["psf_now"]]
            for b in range(1, 6):
                p = s["prices"].get(str(b), {})
                row += [(p.get("buy") or {}).get("median"), (p.get("buy_est") or {}).get("value"),
                        (p.get("rent") or {}).get("median"), (p.get("rent_est") or {}).get("value"),
                        "; ".join(f"{k}: {v}" for k, v in (p.get("flags") or {}).items()) or None]
            row += [s["rating"], s["rating_count"], s["review_count"], "; ".join(s["amenities"]), s["verified"],
                    f"{smallest[0]:.2f} x {smallest[1]:.2f}" if smallest else None, s["url"], s["gmaps"]]
            w.writerow(row)
    with open(unit_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["society_id", "society", "unit", "bhk", "super_sqft", "carpet_sqft", "listed_sqft", "bedrooms_ft", "all_bedrooms_read",
                    "ai_narrowest_bedroom_ft", "ai_every_bedroom_long_side_at_least_ft", "ai_every_bedroom_area_at_least_sqft", "confidence", "source", "page"])
        for s in societies:
            for u in s["unit_types"]:
                w.writerow([s["id"], s["name"], u["label"], u.get("bhk"), u.get("super_sqft"), u.get("carpet_sqft"), u.get("size_sqft"),
                            "; ".join(f"{a:.2f} x {b:.2f}" for a, b in u.get("bedrooms") or []), u.get("rooms_complete"),
                            (u.get("ai") or {}).get("min_short"), (u.get("ai") or {}).get("long_lb"), (u.get("ai") or {}).get("area_lb"),
                            u.get("confidence"), u.get("source"), u.get("page")])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "gurgaon")
