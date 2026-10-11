"""Reproduce the bounded, public OSM reference download (never accepts person data)."""

import argparse
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ASSET_DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "geo"
SOURCE_FILE = "source/osm-kanto-source.json.gz"
MANIFEST_FILE = "manifest.json"
BBOX = (35.43, 139.55, 35.92, 140.04)
ROUTE_IDS = (5195691, 5195692, 3351488, 10312043)
STATION_NAMES = (
    "大宮",
    "浦和",
    "川口",
    "赤羽",
    "上野",
    "秋葉原",
    "神田",
    "東京",
    "品川",
    "蒲田",
    "川崎",
    "鶴見",
    "横浜",
    "錦糸町",
    "新小岩",
    "市川",
    "西船橋",
    "船橋",
    "東神奈川",
    "桜木町",
    "本八幡",
    "下総中山",
    "東船橋",
    "南浦和",
    "北浦和",
    "さいたま新都心",
)
SOURCE_VERSION = "osm-kanto-corridors-20261006-v1"
SOURCE_PRIVACY_POLICY = "routing-only-v1"
ROUTING_TAGS = frozenset({
    "highway", "area", "foot", "access", "motorcar", "motor_vehicle", "vehicle",
    "oneway", "oneway:foot", "oneway:motor_vehicle", "oneway:motorcar",
    "junction", "maxspeed", "barrier", "railway", "type", "except",
    "restriction", "restriction:motorcar",
})
CONDITIONAL_TAGS = frozenset(
    f"{key}:conditional"
    for key in (
        "foot", "access", "motorcar", "motor_vehicle", "vehicle",
        "oneway", "oneway:foot", "oneway:motor_vehicle", "oneway:motorcar",
    )
)


def sanitize_source(source: dict) -> dict:
    """Retain routing topology, access rules and route-stop names, not business metadata."""
    routes = {
        element["id"]: element
        for element in source["elements"]
        if element["type"] == "relation"
        and element["id"] in ROUTE_IDS
        and element.get("tags", {}).get("route") == "train"
    }
    stops = {
        member["ref"]
        for route in routes.values()
        for member in route["members"]
        if member["type"] == "node" and member["role"].startswith("stop")
    }
    elements = []
    for element in source["elements"]:
        clean = {
            key: value
            for key, value in element.items()
            if key in {"type", "id", "lat", "lon", "nodes"}
        }
        if "members" in element:
            clean["members"] = [
                {key: member[key] for key in ("type", "ref", "role")}
                for member in element["members"]
            ]
        tags = element.get("tags", {})
        retained = {
            key: value
            for key, value in tags.items()
            if key in ROUTING_TAGS
            or key in CONDITIONAL_TAGS
            or (
                element["type"] == "relation"
                and tags.get("type") == "restriction"
                and key.startswith("restriction:")
                and "conditional" in key
            )
        }
        if element["type"] == "node" and element["id"] in stops and "name" in tags:
            retained["name"] = tags["name"]
        if element["type"] == "relation" and element["id"] in routes:
            retained.update({"route": "train", "ref": tags["ref"]})
        if retained:
            clean["tags"] = retained
        elements.append(clean)
    return {
        "osm3s": {"timestamp_osm_base": source["osm3s"]["timestamp_osm_base"]},
        "elements": elements,
    }


def sanitize_pinned_source() -> None:
    """Sanitize the local pinned extract without contacting any external service."""
    metadata_path = ASSET_DIRECTORY / MANIFEST_FILE
    metadata = json.loads(metadata_path.read_text())
    source_path = ASSET_DIRECTORY / SOURCE_FILE
    content = source_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != metadata["source_sha256"]:
        raise ValueError("Pinned OSM source checksum differs")
    source = sanitize_source(json.loads(gzip.decompress(content)))
    raw = json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    source_path.write_bytes(compressed)
    metadata.update({
        "source_sha256": hashlib.sha256(compressed).hexdigest(),
        "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
        "source_privacy_policy": SOURCE_PRIVACY_POLICY,
    })
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")


def source_queries() -> list[str]:
    bounds = ",".join(map(str, BBOX))
    names = "|".join(STATION_NAMES[:18])
    ids = ",".join(map(str, ROUTE_IDS))
    prefix = "[out:json][timeout:90][maxsize:268435456];"
    return [
        f"""{prefix}
relation(id:{ids})->.routes;
way(r.routes)({bounds})->.rail;
node[railway=station][name~"^({names})$"]({bounds})->.stations;
(.rail;.stations;node(r.routes)({bounds}););
(._;>;);
out body;
.routes out body;
""",
        f"""{prefix}
way[highway~"^(trunk|primary)(_link)?$"]({bounds})->.roads;
relation(bw.roads)[type=restriction]->.restrictions;
(.roads;>;);
out body;
.restrictions out body;
""",
    ] + [
        f"""{prefix}
node[railway=station][name~"^({"|".join(STATION_NAMES[i : i + 6])})$"]({bounds});
way(around:550)[highway][area!=yes]->.roads;
relation(bw.roads)[type=restriction]->.restrictions;
(.roads;>;);
out body;
.restrictions out body;
"""
        for i in range(0, len(STATION_NAMES), 6)
    ]


def download() -> None:
    """Only explicit CLI invocation contacts a public source; application use is offline."""
    ASSET_DIRECTORY.mkdir(parents=True, exist_ok=True)
    endpoints = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    )
    chunks = []
    queries = source_queries()
    for index, query in enumerate(queries):
        cache = ASSET_DIRECTORY / f"osm-source-part-{index}.json.gz"
        if cache.exists():
            chunk = json.loads(gzip.decompress(cache.read_bytes()))
        else:
            for endpoint in endpoints:
                request = Request(
                    endpoint + "?" + urlencode({"data": query}),
                    headers={"User-Agent": "StaffMatching-public-reference/1.0"},
                )
                try:
                    with urlopen(request, timeout=120) as response:
                        raw = response.read(40_000_001)
                    break
                except (HTTPError, URLError, TimeoutError):
                    if endpoint == endpoints[-1]:
                        raise
            if len(raw) > 40_000_000:
                raise ValueError("Reference response exceeds the per-query 40 MB safety bound")
            chunk = json.loads(raw)
            if chunk.get("remark") or not chunk.get("elements"):
                raise ValueError(f"Incomplete OSM response: {chunk.get('remark')}")
            cache.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        print(f"Part {index}: {len(chunk['elements'])} objects", flush=True)
        chunks.append(chunk)
    elements = {
        (element["type"], element["id"]): element
        for chunk in chunks
        for element in chunk["elements"]
    }
    payload = sanitize_source({
        "osm3s": chunks[0]["osm3s"],
        "elements": sorted(elements.values(), key=lambda e: (e["type"], e["id"])),
    })
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    # A deterministic gzip header makes the downloaded artifact content-addressable.
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    asset = ASSET_DIRECTORY / SOURCE_FILE
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(compressed)
    metadata = {
        "source_version": SOURCE_VERSION,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "osm_timestamp": payload["osm3s"]["timestamp_osm_base"],
        "source_urls": endpoints,
        "queries": queries,
        "part_osm_timestamps": [chunk["osm3s"]["timestamp_osm_base"] for chunk in chunks],
        "bbox_south_west_north_east": BBOX,
        "station_access_radius_m": 550,
        "route_relation_ids": ROUTE_IDS,
        "source_file": SOURCE_FILE,
        "source_sha256": hashlib.sha256(compressed).hexdigest(),
        "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
        "source_privacy_policy": SOURCE_PRIVACY_POLICY,
        "license": "ODbL-1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
        "description": (
            "京浜東北線・中央総武緩行線の実在線路、主要道路、選定駅550m圏の道路。"
            "範囲外・未収録道路・時刻表は対象外。実在の建物への入居を示さない。"
        ),
    }
    (ASSET_DIRECTORY / MANIFEST_FILE).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for index in range(len(queries)):
        (ASSET_DIRECTORY / f"osm-source-part-{index}.json.gz").unlink(missing_ok=True)
    print(f"Downloaded {len(payload['elements'])} OSM objects; {len(compressed):,} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--download", action="store_true")
    actions.add_argument("--sanitize", action="store_true")
    arguments = parser.parse_args()
    if arguments.download:
        download()
    elif arguments.sanitize:
        sanitize_pinned_source()
