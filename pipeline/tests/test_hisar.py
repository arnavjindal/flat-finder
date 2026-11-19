"""Cities without a Magicbricks project list (Hisar): sitemap/project-page discovery, HRERA filings, build merge."""
import json
import pathlib

import build_city
import hrera_list
import mb_parse

FX = pathlib.Path(__file__).parent / "fixtures"


def test_project_path_folds_sub_pages():
    assert mb_parse.project_path("global-spaces-hisar-amenities-rajeev-nagar-hisar-pdpid-4d42") == "global-spaces-hisar-rajeev-nagar-hisar-pdpid-4d42"
    assert mb_parse.project_path("girdhar-rameshwaram-greens-floor-plan-budha-khera-hisar-pdpid-4d43") == "girdhar-rameshwaram-greens-budha-khera-hisar-pdpid-4d43"
    html = '<a href="/laxmi-plaza-photo-videos-urban-estate-ii-hisar-pdpid-4d44">x</a> "\\u002Flaxmi-plaza-urban-estate-ii-hisar-pdpid-4d44"'
    assert mb_parse.project_links(html) == ["laxmi-plaza-urban-estate-ii-hisar-pdpid-4d44"]


def test_project_identity_reads_city():
    p = mb_parse.project_identity((FX / "mb_project_flat.html").read_text(encoding="utf-8", errors="ignore"))
    assert p["name"] == "Ireo The Corridors" and p["city_id"] == "2951" and p["locality"] == "Sector 67A"
    assert p["url"].endswith("-pdpid-4d4235303231363539")


def page(*lines):
    return "".join(f"<td>{l}</td>" for l in lines)


NEW_FORM = page("1. Name of the project", "DIGANTIKA RESIDENCY", "2. Address of the site of the project", "(Annex a copy in Folder A)",
                "VILLAGE BIR, SECTOR 39, HISAR", "Tehsil", "HISAR", "1. Land area of the project", "9.85 (Acre)",
                "PLOTS TO BE SOLD", "5.27390", "LAND AREA TO BE USED FOR CONSTRUCTION OF APARTMENTS", "0",
                "iii) Sizes of the plots to be offered in the project", "Sr. No", "Plot Area(In Square Meter)", "Number of plots in the project",
                "1", "144.02", "10", "2", "141.18", "7", "Total", "17",
                "iv. Type of apartments to be constructed in the project:", "Type", "Carpet area(In Square Meter)", "Number of apartments",
                "Number of towers", "Apartment/Shops/Other Buildings", "0", "0", "0", "(vi) Quarterly schedule",
                "ii) Likely date of completing the project", "21-08-2030")

ONGOING_FORM = page("1. Name of the project", "REGENCY PARK", "1. Land area of the project", "12.081 (Acre)",
                    "LAND AREA TO BE USED FOR CONSTRUCTION OF APARTMENTS", "7.118",
                    "No. of towers to be/ being constructed for booked apartments",
                    "1", "APARTMENT/SHOPS/OTHER BUILDINGS Type 1", "61.269", "187", "11457.303", "80", "107", "2",
                    "2", "APARTMENT/SHOPS/OTHER BUILDINGS Type 5", "137.069", "36", "4934.484", "0", "36",
                    "Number of apartments booked/ sold")


def test_hrera_new_project_form():
    d = hrera_list.parse_project(NEW_FORM)
    assert d["site_address"] == "VILLAGE BIR, SECTOR 39, HISAR" and d["land_acres"] == 9.85
    assert d["plots"] == [{"plot_sqm": 144.02, "count": 10}, {"plot_sqm": 141.18, "count": 7}]
    assert d["apartments"] == [] and d["completion"] == "21-08-2030"
    assert hrera_list.kind_of({**d, "name": "DIGANTIKA RESIDENCY"}) == "plot"


def test_hrera_ongoing_project_form():
    d = hrera_list.parse_project(ONGOING_FORM)
    assert d["apartments"] == [{"type": "Type 1", "carpet_sqm": 61.269, "count": 187, "towers": 2},
                               {"type": "Type 5", "carpet_sqm": 137.069, "count": 36, "towers": None}]
    assert hrera_list.kind_of({**d, "name": "REGENCY PARK"}) == "apartment"


def test_hrera_shops_and_industry_are_commercial():
    shops = {"name": "CONNAUGHT PLACE", "apartments": [{"type": "Type SHOP"}], "plots": []}
    assert hrera_list.kind_of(shops) == "commercial"
    assert hrera_list.kind_of({"name": "MKS UDYOG CITY", "apartments": [], "plots": [{"plot_sqm": 500, "count": 3}]}) == "commercial"


def test_filed_units_keep_filed_values():
    h = {"url": "https://haryanarera.gov.in/view_project/project_preview_open/1",
         "apartments": [{"type": "Type 1", "carpet_sqm": 61.269, "count": 187, "towers": 2}], "plots": [{"plot_sqm": 144.02, "count": 10}]}
    apt, plot = build_city.filed_units(h)
    assert apt["carpet_sqft"] == round(61.269 * 10.7639) and "61.269 sq m carpet" in apt["label"] and apt["confidence"] == "filing"
    assert plot["label"] == "Plot 144.02 sq m · 10 filed" and not plot.get("size_sqft")
    assert build_city.sector_label("VILLAGE BIR, SECTOR 39, HISAR") == "Sector 39"


def test_add_hrera_attaches_places_or_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(build_city, "OUT", tmp_path)
    (tmp_path / "hisar").mkdir()
    base = {"promoter": "X", "rera": "", "reg": "", "apartments": [], "plots": [], "kind": "plot", "status": None}
    filed = [{**base, "name": "AARCITY REGENCY PARK", "address": "SATROD", "url": "https://h/1", "rera": "RERA-PKL-1"},
             {**base, "name": "SHREE KRISHNA ENCLAVE", "address": "SECTOR 33, HISAR", "url": "https://h/2"},
             {**base, "name": "OM ESTATES", "address": "NH-9", "url": "https://h/3"},
             {**base, "name": "GREEN ENCLAVE", "address": "SECTOR 9, HANSI", "url": "https://h/4"}]
    (tmp_path / "hisar" / "hrera_projects.json").write_text(json.dumps(filed))
    ring = [[75.70, 29.15], [75.71, 29.15], [75.71, 29.16], [75.70, 29.16], [75.70, 29.15]]
    areas = [{"properties": {"name": "Shree Krishna Enclave", "osm": "way/9"}, "geometry": {"coordinates": [ring]}},
             {"properties": {"name": "Sainik Adarsh Enclave", "osm": "way/8"}, "geometry": {"coordinates": [ring]}}]
    projects = [{"mb_id": "1", "name": "Aarcity Regency Park", "locality": "Satrod", "lat": 29.2, "lng": 75.76, "url": "https://mb/1"}]
    attached, added, unplaced = build_city.add_hrera("hisar", {}, projects, areas, lambda p: True)
    assert attached == 1 and projects[0]["hrera_url"] == "https://h/1" and projects[0]["rera_id"] == "RERA-PKL-1"
    assert [a["name"] for a in added] == ["Shree Krishna Enclave"] and added[0]["placed_by"]["osm"] == "way/9"
    assert added[0]["locality"] == "Sector 33" and unplaced == 2  # Om Estates, and Green Enclave is not Sainik Adarsh Enclave


def card(**kw):
    base = {"encId": "x", "id": "1", "ct": "2956", "transType": "Sale", "isProjProp": "N", "lmtDName": "Sector 13"}
    return {**base, **kw}


def test_listing_record_reads_sizes_from_its_title():
    r = mb_parse.listing_record(card(propTypeD="Residential Plot", url="825-Sq-yrd-Residential-Plot-FOR-Sale-Ladwa-Road-in-Hisar&id=4d42",
                                     price=16000000, priceD="1.60 Cr", ltcoordGeo="29.10231,75.76439"))
    assert r["kind"] == "plot" and r["deal"] == "sale" and r["area"] == 825 and r["area_unit"] == "Sq yrd"
    assert r["built_sqft"] is None and r["locality_point"] == [29.10231, 75.76439] and r["pin"] is None
    assert r["url"] == "https://www.magicbricks.com/propertyDetails/825-Sq-yrd-Residential-Plot-FOR-Sale-Ladwa-Road-in-Hisar&id=4d42"
    house = mb_parse.listing_record(card(propTypeD="Residential House", transType="Rent", bedroomD="2", caSqFt=2450,
                                         url="2-BHK-2450-Sq-ft-Residential-House-FOR-Rent-Azad-Nagar-in-Hisar&id=4d43"))
    assert house["kind"] == "house" and house["deal"] == "rent" and house["bhk"] == 2 and house["built_sqft"] == 2450
    assert mb_parse.listing_record(card(propTypeD="Warehouse/ Godown", url="1000-Sq-ft-Warehouse-FOR-Rent")) is None


def test_add_listings_places_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(build_city, "OUT", tmp_path)
    (tmp_path / "hisar").mkdir()
    city_default = [29.149188, 75.721653]
    rec = {"in_project": False, "kind": "house", "type": "Residential House", "deal": "sale", "price": 9e6, "price_text": "90 Lac",
           "bhk": 3, "area": 200, "area_unit": "Sq yrd", "built_sqft": 1800, "carpet": None, "posted": "Nov 01, '26", "seller": "Owner",
           "status": None, "floor": None, "furnished": None, "image": None, "url": "https://mb/l"}
    recs = [{**rec, "id": "1", "locality": "Sector 13", "locality_point": [29.1321, 75.7390], "pin": [29.1321, 75.7390]},  # pin = locality
            {**rec, "id": "2", "locality": "Sector 13", "locality_point": [29.1321, 75.7390], "pin": None},
            {**rec, "id": "3", "locality": "Sector 14", "locality_point": [29.1400, 75.7500], "pin": [29.1450, 75.7550]},  # its own pin
            {**rec, "id": "4", "locality": "Camry road", "locality_point": city_default, "pin": None},
            {**rec, "id": "5", "locality": "Chandigarh road", "locality_point": city_default, "pin": None},
            {**rec, "id": "6", "locality": "Mill gate road", "locality_point": None, "pin": city_default}]
    (tmp_path / "hisar" / "mb_listings.json").write_text(json.dumps(recs))
    out, unplaced, pinned = build_city.add_listings("hisar", lambda p: True)
    assert unplaced == 3 and pinned == 1  # three share the city default point: not real locations
    by = {o["mb_id"]: o for o in out}
    assert by["l3"]["listing"]["location"] == "pin" and (by["l3"]["lat"], by["l3"]["lng"]) == (29.145, 75.755)
    assert by["l1"]["listing"]["location"] == by["l2"]["listing"]["location"] == "locality"
    assert (by["l1"]["lat"], by["l1"]["lng"]) != (by["l2"]["lat"], by["l2"]["lng"])  # spread apart, still near Sector 13
    assert all(abs(by[k]["lat"] - 29.1321) < 0.001 for k in ("l1", "l2"))
    assert by["l1"]["name"] == "3 BHK house, Sector 13" and by["l1"]["kind_fixed"] == "house"
