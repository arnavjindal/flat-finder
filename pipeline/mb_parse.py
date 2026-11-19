"""Parse Magicbricks list and project pages from their embedded JSON state."""
import json
import re

_DEC = json.JSONDecoder()


def _json_after(html, marker):
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("=", i) + 1
    obj, _ = _DEC.raw_decode(html[j:].lstrip())
    return obj


def parse_total(html):
    m = re.search(r'"totalCount":(\d+)', html)
    return int(m.group(1)) if m else None


def parse_list_page(html):
    """Return the project cards on a list page (dicts with psmName, pdpUrl, ...)."""
    cards = []
    for m in re.finditer(r'"psmName":', html):
        # walk back to the start of the enclosing object
        j = html.rfind("{", 0, m.start())
        while j > 0:
            try:
                obj, _ = _DEC.raw_decode(html[j:])
                if "psmName" in obj and "pdpUrl" in obj:
                    cards.append(obj)
                    break
            except ValueError:
                pass
            j = html.rfind("{", 0, j)
    seen, out = set(), []
    for c in cards:
        if c["psmid"] not in seen:
            seen.add(c["psmid"])
            out.append(c)
    return out


def _num(v):
    try:
        f = float(v)
        return f if f == f and f != 0 else None
    except (TypeError, ValueError):
        return None


def photo_links(paths, n=8):
    """Magicbricks photo paths -> [{thumb, full}] (thumbnails are a resized copy Magicbricks serves)."""
    out = []
    for p in (paths or "").split(","):
        p = p.strip()
        if not p or "." not in p or "Floor-Plan" in p:
            continue
        stem, ext = p.rsplit(".", 1)
        base = "https://img.staticmb.com/mbimages/project/"
        out.append({"thumb": f"{base}Photo_h310_w462/{stem}_310_462.{ext}", "full": base + p})
    return out[:n]


def card_to_project(c):
    return {
        "mb_id": c.get("psmid"),
        "name": (c.get("psmName") or "").strip(),
        "developer": c.get("devName"),
        "locality": c.get("lmtDName"),
        "pincode": c.get("pincode"),
        "status": c.get("showReadyToMove"),
        "possession": c.get("prjPossYear"),
        "units": c.get("totalUnits") or None,
        "towers": c.get("noTower") or None,
        "acres": c.get("projArea") if c.get("projAreaUnitDesc") == "Acre" else None,
        "price_min": _num(c.get("seoSMinPrice")),
        "price_max": _num(c.get("seoSMaxPrice")),
        "prop_types": c.get("psmcndproptypes"),
        "brochure_url": c.get("sblink"),
        "card_units": parse_unit_info(c.get("unitInfo") or ""),
        "images": photo_links(c.get("image")),
        "type_codes": [t.strip() for t in str(c.get("ty") or "").split(",") if t.strip()],
        "url": "https://www.magicbricks.com/" + c["pdpUrl"],
    }


def project_identity(html):
    """Index record read from a project page itself, for cities with no Magicbricks project list."""
    state = _json_after(html, "window.SERVER_PRELOADED_STATE_") or {}
    mob = (state.get("projectDetailData") or {}).get("prjMobileBean") or {}
    info = mob.get("infoBean") or {}
    if not mob.get("psmId") or not mob.get("projectUrl"):
        return None
    devs = mob.get("projectDeveloperList") or []
    devs = [devs] if isinstance(devs, dict) else devs  # one object or a list, depending on the page
    return {
        "mb_id": str(mob["psmId"]),
        "name": (info.get("projectName") or mob.get("opsmname") or "").strip(),
        "developer": next((d["developerName"] for d in devs if isinstance(d, dict) and d.get("developerName")), None),
        "locality": info.get("lmtDName") or info.get("localityName"),
        "pincode": (mob.get("detailBean") or {}).get("pincode"),
        "city_id": str(info.get("cityId") or ""),
        "type_codes": [t.strip() for t in str(info.get("psmCndPropTypes") or "").split(",") if t.strip()],
        "url": "https://www.magicbricks.com/" + mob["projectUrl"].lstrip("/"),
    }


_SUBPAGE = re.compile(r"-(?:amenities|floor-plans?|location|photo-videos|photos?|reviews?|brochure|rera-details|price-list)(?=-)")


def project_path(path):
    """Main project page path for a project or one of its sub-pages (amenities, floor plan, location, ...)."""
    return _SUBPAGE.sub("", path, count=1)


def project_links(html):
    """Main project page paths linked from a page (sub-page links folded in), one per project id."""
    out = {}
    for path, pid in re.findall(r"/([a-z0-9-]+-pdpid-([0-9a-f]+))", html.replace("\\u002F", "/")):
        out.setdefault(pid, project_path(path))
    return list(out.values())


LISTING_KIND = {"Residential House": "house", "Villa": "villa", "Builder Floor Apartment": "builder floor",
                "Multistorey Apartment": "apartment", "Apartment": "apartment", "Penthouse": "apartment",
                "Studio Apartment": "apartment", "Residential Plot": "plot", "Agricultural Land": "farmland"}
_LISTING_URL = re.compile(r"^(?:(\d+)-BHK-)?([\d.]+)-(.+?)-(?:Residential|Builder|Multistorey|Agricultural|Villa|Apartment|Penthouse|Studio)")


def parse_listing_cards(html):
    """Every property card on a Magicbricks listing (search) page, sale or rent, as raw dicts."""
    out, seen = [], set()
    for m in re.finditer(r'"encId"\s*:', html):
        j = html.rfind("{", 0, m.start())
        while j > 0:
            try:
                obj, _ = _DEC.raw_decode(html[j:])
                if "encId" in obj and obj.get("id") and obj["id"] not in seen:
                    seen.add(obj["id"])
                    out.append(obj)
                break
            except ValueError:
                j = html.rfind("{", 0, j)
    return out


def _point(lat, lng):
    lat, lng = _num(lat), _num(lng)
    return [round(lat, 6), round(lng, 6)] if lat and lng else None


def listing_record(c):
    """A residential or farmland listing card -> record with sizes as printed; None for other property types."""
    kind = LISTING_KIND.get(c.get("propTypeD"))
    if not kind or not c.get("url"):
        return None
    m = _LISTING_URL.match(c["url"])  # the listing's own title, e.g. 825-Sq-yrd-Residential-Plot-FOR-Sale-...
    geo = (c.get("ltcoordGeo") or "").split(",")
    bhk = c.get("bedroomD")
    return {
        "id": str(c["id"]), "kind": kind, "type": c["propTypeD"], "deal": "rent" if c.get("transType") == "Rent" else "sale",
        "price": _num(c.get("price")), "price_text": c.get("priceD"),
        "bhk": int(bhk) if str(bhk or "").isdigit() else None, "baths": c.get("bathD"),
        "area": _num(m.group(2)) if m else None, "area_unit": m.group(3).replace("-", " ") if m else None,
        "built_sqft": _num(c.get("caSqFt")) if kind not in ("plot", "farmland") else None,
        "carpet": f"{c['carpetArea']} {c.get('carpAreaUnit') or ''}".strip() if c.get("carpetArea") else None,
        "locality": c.get("lmtDName"), "pin": _point(c.get("pmtLat"), c.get("pmtLong")),
        "locality_point": _point(*geo) if len(geo) == 2 else None,
        "posted": (c.get("postedLabelD") or "").replace("Posted:", "").strip() or None, "seller": c.get("userType"),
        "status": c.get("possStatusD"), "floor": c.get("floorD"), "furnished": c.get("furnishedD"),
        "title": c.get("propertyTitle") or c.get("auto_desc"), "in_project": c.get("isProjProp") == "Y",
        "city_id": str(c.get("ct") or ""), "image": (c.get("image") or "").split(",")[0] or None,
        "url": "https://www.magicbricks.com/propertyDetails/" + c["url"],
    }


def parse_project_page(html):
    """Return details from a project page: coordinates, RERA, amenities, towers, unit configs."""
    state = _json_after(html, "window.SERVER_PRELOADED_STATE_") or {}
    pdd = state.get("projectDetailData") or {}
    mob = pdd.get("prjMobileBean") or {}
    info = mob.get("infoBean") or {}
    det = mob.get("detailBean") or {}
    lat, lng = _num(info.get("psmLatitude")), _num(info.get("psmLongitude"))
    amen = [a.get("amenityName") for a in (mob.get("amenitiesList") or []) if a.get("amenityName")]
    return {
        "lat": lat,
        "lng": lng,
        "rera_id": det.get("reraId"),
        "towers": _num(det.get("towers")),
        "units": _num(det.get("units")),
        "status": det.get("status"),
        "amenities": amen,
        "config_text": det.get("prjConfig"),
        "configs": parse_configs(state),
        **parse_extras(html),
    }


def parse_unit_info(raw):
    """'2 , 10002 , 1484  Sq-ft | ...' -> [{bhk, sqft, kind}]; entries without a size are dropped."""
    units = []
    for part in raw.split("|"):
        bits = [b.strip() for b in part.split(",")]
        if len(bits) != 3:
            continue
        m = re.match(r"([\d.]+)\s*Sq-ft", bits[2])
        if not m:
            continue
        units.append({
            "bhk": int(bits[0]) if bits[0].isdigit() else None,
            "sqft": float(m.group(1)),
            "kind": bits[1],
        })
    return units


def parse_configs(state):
    """Unit types from mUnitInfo plus per-BHK prices and plan image links."""
    pdd = state.get("projectDetailData") or {}
    units = parse_unit_info(pdd.get("mUnitInfo") or "")
    pricing = (state.get("projectPageSeoStaticData") or {}).get("pdpUnitPricing") or {}
    plans = {}
    for key, rows in pricing.items():
        mm = re.match(r"(\d+) BHK", key)
        if not mm or not isinstance(rows, list):
            continue
        b = int(mm.group(1))
        plans[b] = {
            "price_min": min((r.get("minPriceD") or 0) for r in rows) or None,
            "price_max": max((r.get("maxPriceD") or 0) for r in rows) or None,
            "plan_images": sorted({"https://img.staticmb.com/mbimages/project/" + r["unitFlrPlanImg"]
                                   for r in rows if r.get("unitFlrPlanImg")}),
        }
    return {"units": units, "by_bhk": plans}


_TAG = re.compile(r"<[^>]+>")


def _text(html, n):
    t = re.sub(r"\s+", " ", _TAG.sub(" ", html or "")).replace("&nbsp;", " ").replace("&amp;", "&").strip()
    return t[:n].rsplit(" ", 1)[0] + "…" if len(t) > n else t


def _listing(r):
    try:
        bhk = int(r.get("bedroomD")) if str(r.get("bedroomD") or "").isdigit() else None
    except ValueError:
        bhk = None
    return {"bhk": bhk, "price": _num(r.get("price")), "sqft": _num(r.get("caSqFt")), "psf": _num(r.get("sqFtPrice")),
            "furnished": r.get("furnishedD"), "floor": r.get("floorNo"), "posted": (r.get("postDateT") or "")[:10],
            "type": r.get("propTypeD")}


RATING_KEYS = {"waterRt": "water", "electricityRt": "power", "backupRt": "power backup", "securityRt": "security",
               "maintenanceRt": "maintenance", "constQualityRt": "construction", "parkingRt": "parking", "sportsRt": "sports",
               "gardenRt": "green areas", "shopsRt": "shops nearby", "staffRt": "staff", "sewageRt": "sewage"}


def parse_extras(html):
    """Ratings, resident reviews, live sale/rent listings, listing counts and price trend from a project page."""
    state = _json_after(html, "window.SERVER_PRELOADED_STATE_") or {}
    seo = state.get("projectPageSeoStaticData") or {}
    mob = (state.get("projectDetailData") or {}).get("prjMobileBean") or {}
    rb = mob.get("ratingBean") or {}
    groups = (((seo.get("bhkDetailsDTO") or {}).get("bhkProjectDetailsMap") or {}).get("ALL") or {})
    reviews, seen = [], set()
    for r in (seo.get("projectReviews") or {}).get("resultList") or []:
        if r.get("isSponsered") in (True, "Y", "true", 1) or len(reviews) >= 6:
            continue
        text = _text(r.get("reviewDescription"), 320)
        key = re.sub(r"\W+", "", text.lower())[:60]
        if text and key not in seen:
            seen.add(key)
            reviews.append({"title": _text(r.get("reviewTitle"), 90), "text": text, "date": r.get("reviewedOn"),
                            "by": r.get("userType"), "rating": r.get("overAllRating")})
    trend = []
    for doc in (seo.get("projectTrendsGraph") or {}).get("projectSrpPriceTrendDoc") or []:
        try:
            data = json.loads(doc.get("jsonProjectData") or "{}")
        except ValueError:
            continue
        for v in (data or {}).values():
            for q in (((v or {}).get("yoyGrowth") or {}).get("fiveY") or []):
                if q.get("qAvgP"):
                    trend.append([q.get("qIdentifier"), q["qAvgP"]])
    m = re.search(r"₹\s?([\d,]+)", mob.get("priceTrendsCont") or "")
    loc = (seo.get("localityDetailsBean") or {}).get("localityDetailDTO") or {}
    return {
        "rating": _num(rb.get("psmAvgRt")),
        "rating_count": int(rb["totalRating"]) if str(rb.get("totalRating") or "").isdigit() else None,
        "review_count": int(rb["totalReview"]) if str(rb.get("totalReview") or "").isdigit() else None,
        "ratings": {name: rb[k] for k, name in RATING_KEYS.items() if rb.get(k)},
        "reviews": reviews,
        "sale_count": int(mob["sellCountProperty"]) if str(mob.get("sellCountProperty") or "").isdigit() else None,
        "rent_count": int(mob["rentCountProperty"]) if str(mob.get("rentCountProperty") or "").isdigit() else None,
        "sale_listings": [_listing(r) for r in groups.get("groupedResult") or [] if r.get("price")],
        "rent_listings": [_listing(r) for r in groups.get("groupedRentResult") or [] if r.get("price")],
        "psf_now": float(m.group(1).replace(",", "")) if m else None,
        "trend": trend,
        "locality_rating": _num(loc.get("ratingAvgCount")),
        "description": _text((mob.get("projectDescription") or {}).get("fullDesc"), 600),
    }
