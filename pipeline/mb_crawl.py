"""Polite, cached, resumable Magicbricks crawler.

usage: python mb_crawl.py list <city>      # all list pages -> out/<city>/mb_index.json
       python mb_crawl.py discover <city>  # instead of list, for cities with no project list (Hisar)
       python mb_crawl.py projects <city>  # every project page -> out/<city>/mb_projects.json
<city> is a key in data/cities.json (default gurgaon).
"""
import gzip
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.request

import mb_parse

ROOT = pathlib.Path(__file__).parent
CACHE = ROOT / "cache" / "mb"
OUT = ROOT / "out"
CITIES = json.loads((ROOT.parent / "data" / "cities.json").read_text())
CITY = next((a for a in sys.argv[1:] if a in CITIES), "gurgaon")
CFG = CITIES[CITY]
LIST_URL = "https://www.magicbricks.com/residential-projects-in-" + CFG["magicbricks"] + "-nprid/page-{}"
OUT = OUT / CITY
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
DELAY = 1.0


def fetch(url):
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".html.gz")
    if f.exists() and f.stat().st_size > 2000:
        return gzip.decompress(f.read_bytes()).decode("utf-8", "ignore")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            html = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "ignore")
            f.write_bytes(gzip.compress(html.encode("utf-8"), 9))
            time.sleep(DELAY)
            return html
        except Exception as e:  # network hiccup: back off and retry
            print("  retry", attempt + 1, url, e, flush=True)
            time.sleep(5 * (attempt + 1))
    return None


def crawl_list():
    first = fetch(LIST_URL.format(1))
    total = mb_parse.parse_total(first)
    pages = -(-total // 20)
    projects = {}
    for n in range(1, pages + 1):
        html = fetch(LIST_URL.format(n))
        if not html:
            continue
        for c in mb_parse.parse_list_page(html):
            projects[c["psmid"]] = mb_parse.card_to_project(c)
        print(f"list {n}/{pages}: {len(projects)} projects", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mb_index.json").write_text(json.dumps(list(projects.values()), indent=0))
    print("total reported", total, "collected", len(projects))


def crawl_projects():
    index = json.loads((OUT / "mb_index.json").read_text())
    from concurrent.futures import ThreadPoolExecutor

    def one(p):
        html = fetch(p["url"])
        try:
            detail = mb_parse.parse_project_page(html) if html else {}
        except Exception as e:
            print("  parse failed", p["url"], e, flush=True)
            detail = {}
        return {**p, **{k: v for k, v in detail.items() if v not in (None, [], "")}}

    out = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for i, merged in enumerate(ex.map(one, index), 1):
            out.append(merged)
            if i % 100 == 0 or i == len(index):
                print(f"project {i}/{len(index)}", flush=True)
                (OUT / "mb_projects.json").write_text(json.dumps(out, indent=0))


SITEMAPS = re.compile(r"https://www\.magicbricks\.com/(?:pdp_[a-z][A-Za-z0-9_-]*|prellaProjects|residential_plots_projects|villas_projects)\.xml\.gz")


def sitemap_projects(slug):
    """Project pages in a city from Magicbricks' public project sitemaps (sitemap_index.xml, named in robots.txt).
    The per-city sitemaps only cover big cities; the project sub-page ones (amenities, location, ...) cover all."""
    d = CACHE / "sitemaps"
    d.mkdir(parents=True, exist_ok=True)
    index = fetch("https://www.magicbricks.com/sitemap_index.xml") or ""
    paths = {}
    for url in sorted(set(SITEMAPS.findall(index))):
        f = d / url.rsplit("/", 1)[-1]
        if not f.exists():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                f.write_bytes(urllib.request.urlopen(req, timeout=120).read())
            except Exception as e:
                print("  sitemap failed", url, e, flush=True)
                continue
            time.sleep(DELAY)
        xml = gzip.decompress(f.read_bytes()).decode("utf-8", "ignore")
        for path, pid in re.findall(rf"<loc>https://www\.magicbricks\.com/([a-z0-9-]+-{slug}-pdpid-([0-9a-f]+))</loc>", xml):
            paths.setdefault(pid, mb_parse.project_path(path))
    return list(paths.values())


def crawl_discover(max_lists=25, max_projects=2000):
    """For cities with no Magicbricks project list (Hisar): take the city's projects from the project sitemaps,
    add those linked from the first listing pages (their cards carry unit sizes and brochures), then follow the
    nearby-project links between project pages. A project is kept only if its own page puts it in this city."""
    slug, city_id = CFG["magicbricks"], str(CFG["magicbricks_city_id"])
    base = "https://www.magicbricks.com/"
    proj_paths = dict.fromkeys(sitemap_projects(slug))
    print(f"projects in the sitemaps: {len(proj_paths)}", flush=True)
    listing = re.compile(rf"/((?:[a-z0-9]+-)*{slug}(?:-[a-z0-9]+)*-ppp(?:fs|fr))(?![a-z0-9-])")
    lists = [f"flats-in-{slug}-for-sale-pppfs"]
    seen_lists, cards = set(lists), {}
    while lists and len(seen_lists) - len(lists) < max_lists:
        html = fetch(base + lists.pop(0))
        if not html:
            continue
        for c in mb_parse.parse_list_page(html):
            cards[str(c["psmid"])] = mb_parse.card_to_project(c)
        for p in mb_parse.project_links(html):
            proj_paths.setdefault(p, None)
        for p in listing.findall(html.replace("\\u002F", "/")):
            if p not in seen_lists:
                seen_lists.add(p)
                lists.append(p)
    print(f"listing pages read {len(seen_lists) - len(lists)}, projects linked so far {len(proj_paths)}", flush=True)

    projects, todo, other_city = {}, list(proj_paths), 0
    seen = set(todo)
    while todo and len(projects) < max_projects:
        path = todo.pop(0)
        html = fetch(base + path)
        ident = mb_parse.project_identity(html) if html else None
        if not ident or ident["city_id"] != city_id:
            other_city += bool(ident)
            continue
        card = cards.get(ident["mb_id"], {})
        projects[ident["mb_id"]] = {**ident, **{k: v for k, v in card.items() if v not in (None, [], "")}}
        for p in mb_parse.project_links(html):
            if slug in p.split("-") and p not in seen:  # nearby projects in the same city
                seen.add(p)
                todo.append(p)
        if len(projects) % 20 == 0:
            print(f"projects in {CFG['name']}: {len(projects)}, queued {len(todo)}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mb_index.json").write_text(json.dumps(list(projects.values()), indent=0))
    print(f"projects in {CFG['name']}: {len(projects)}, skipped as another city: {other_city}")


LISTING_PAGES = ["independent-house-for-sale-in-{s}-pppfs", "villa-for-sale-in-{s}-pppfs", "builder-floor-for-sale-in-{s}-pppfs",
                 "flats-in-{s}-for-sale-pppfs", "residential-plots-land-for-sale-in-{s}-pppfs", "agricultural-land-for-sale-in-{s}-pppfs",
                 "independent-house-for-rent-in-{s}-pppfr", "flats-for-rent-in-{s}-pppfr", "villa-for-rent-in-{s}-pppfr",
                 "owner-property-for-sale-in-{s}-pppfs", "owner-property-for-rent-in-{s}-pppfr"]  # owner pages carry the plots
RESIDENTIAL = re.compile(r"house|villa|floor|flats?|apartment|plot|land|bungalow|property")
SKIP_PAGE = re.compile(r"commercial|office|shop|showroom|warehouse|godown|industrial|hostel|pg-|room|price|lakh|crore|budget|luxury|bhk|resale|ready|furnished")


def crawl_listings(max_pages=250):
    """Individual listings (houses, villas, builder floors, flats, plots, farmland; sale and rent), for cities where
    most homes are not in projects (Hisar). Walks the city's listing pages and their /page-N, plus the sector and
    locality listing pages they link. -> out/<city>/mb_listings.json (cards of this city only, sizes as printed)."""
    slug, city_id = CFG["magicbricks"], str(CFG["magicbricks_city_id"])
    base = "https://www.magicbricks.com/"
    local = re.compile(rf"/((?:[a-z0-9]+-)*{slug}(?:-[a-z0-9]+)*-ppp(?:fs|fr))(?![a-z0-9-])")
    todo = [p.format(s=slug) for p in LISTING_PAGES]
    seen, recs, pages = set(todo), {}, 0
    while todo and pages < max_pages:
        path = todo.pop(0)
        html = fetch(base + path)
        pages += 1
        if not html:
            continue
        before = len(recs)
        for c in mb_parse.parse_listing_cards(html):
            r = mb_parse.listing_record(c)
            if r and r["city_id"] == city_id:
                recs.setdefault(r["id"], r)
        page = re.search(r"/page-(\d+)$", path)
        nxt = f"{path.split('/page-')[0]}/page-{int(page.group(1)) + 1 if page else 2}"
        if len(recs) > before and nxt.split("/", 1)[-1] in html.replace("\\u002F", "/"):
            todo.append(nxt)  # this page still added listings and links the next one
        if not page:
            for p in local.findall(html.replace("\\u002F", "/")):
                if p not in seen and RESIDENTIAL.search(p) and not SKIP_PAGE.search(p):
                    seen.add(p)
                    todo.append(p)
        if pages % 20 == 0:
            print(f"listing pages {pages}, queued {len(todo)}, listings {len(recs)}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mb_listings.json").write_text(json.dumps(list(recs.values()), indent=0))
    from collections import Counter
    print(f"listing pages {pages}, listings in {CFG['name']}: {len(recs)}", Counter(f"{r['kind']} {r['deal']}" for r in recs.values()))


if __name__ == "__main__":
    {"list": crawl_list, "projects": crawl_projects, "discover": crawl_discover, "listings": crawl_listings}[sys.argv[1]]()
