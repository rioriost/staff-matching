"""Public-only OSM graph derivation. No private coordinates are written to these assets."""

import gzip
import hashlib
import heapq
import json
import math
from collections import defaultdict

from backend.saas_geo_build import (
    ASSET_DIRECTORY,
    BBOX,
    MANIFEST_FILE,
    SOURCE_FILE,
    SOURCE_VERSION,
)

NETWORK_VERSION = SOURCE_VERSION + "-graph-v1"
ASSUMPTIONS_VERSION = "commute-conservative-20261006-v1"
WALK_METRES_PER_SECOND = 1.2
RAIL_METRES_PER_SECOND = 11.11
GRAPH_FILE = "network.json.gz"
BASEMAP_FILE = "basemap.geojson"
LICENSE_FILE = "LICENSE.txt"
PUBLIC_GEOGRAPHY_FILES = (GRAPH_FILE, BASEMAP_FILE, MANIFEST_FILE, LICENSE_FILE)
DENIED = {"no", "private", "destination", "customers", "delivery", "permit"}
FOOT_HIGHWAYS = {
    "footway",
    "pedestrian",
    "path",
    "steps",
    "living_street",
    "residential",
    "unclassified",
    "service",
    "tertiary",
    "secondary",
    "primary",
    "tertiary_link",
    "secondary_link",
    "primary_link",
}
CAR_HIGHWAYS = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "motorway_link",
    "trunk_link",
    "primary_link",
    "secondary_link",
    "tertiary_link",
}


def distance_metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    lat_delta = lat2 - lat1
    lon_delta = math.radians(b[1] - a[1])
    value = math.sin(lat_delta / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin(lon_delta / 2) ** 2
    )
    return 12_742_000 * math.asin(min(1.0, math.sqrt(value)))


def permitted(tags: dict, mode: str) -> bool:
    keys = (
        ("foot", "access")
        if mode == "walk"
        else (
            "motorcar",
            "motor_vehicle",
            "vehicle",
            "access",
        )
    )
    if any(f"{key}:conditional" in tags for key in keys):
        return False
    direction_keys = (
        ("oneway:foot",)
        if mode == "walk"
        else (
            "oneway",
            "oneway:motor_vehicle",
            "oneway:motorcar",
        )
    )
    if any(
        f"{key}:conditional" in tags
        or tags.get(key, "no") not in {"yes", "no", "1", "-1", "0", "true", "false"}
        for key in direction_keys
    ):
        return False
    for key in keys:
        if key in tags:
            return tags[key] not in DENIED
    return True


def directions(tags: dict, mode: str) -> tuple[bool, bool]:
    value = (
        tags.get("oneway:foot", "no")
        if mode == "walk"
        else tags.get(
            "oneway:motorcar",
            tags.get(
                "oneway:motor_vehicle",
                tags.get("oneway", "yes" if tags.get("junction") == "roundabout" else "no"),
            ),
        )
    )
    return value != "-1", value not in {"yes", "1", "true"}


def shortest_path(adjacency: dict, start: int, finish: int) -> tuple[float, list[int]] | None:
    queue = [(0.0, start)]
    costs = {start: 0.0}
    previous = {}
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != costs[node]:
            continue
        if node == finish:
            path = [node]
            while path[-1] != start:
                path.append(previous[path[-1]])
            return cost, path[::-1]
        for neighbour, length in adjacency.get(node, ()):
            candidate = cost + length
            if candidate < costs.get(neighbour, math.inf):
                costs[neighbour] = candidate
                previous[neighbour] = node
                heapq.heappush(queue, (candidate, neighbour))
    return None


def graph_feature_collection(graph: dict) -> dict:
    """Build a public-only approximate map, independently of generated records."""
    points = {node: (lat, lon) for node, lat, lon in graph["nodes"]}
    features = []
    seen_segments = set()
    for edge in graph["rail_edges"]:
        key = tuple(sorted((edge["from_name"], edge["to_name"])))
        if key in seen_segments:
            continue
        seen_segments.add(key)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": "rail",
                    "line": edge["line"],
                    "from": edge["from_name"],
                    "to": edge["to_name"],
                    "osm_relation": edge["relation_id"],
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[points[node][1], points[node][0]] for node in edge["nodes"]],
                },
            }
        )
    for road in graph["map_roads"]:
        nodes = road["nodes"]
        simplified = nodes[::3] + ([nodes[-1]] if nodes[-1] != nodes[::3][-1] else [])
        features.append(
            {
                "type": "Feature",
                "properties": {"kind": "road", "name": road["name"], "osm_way": road["way_id"]},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[points[node][1], points[node][0]] for node in simplified],
                },
            }
        )
    for station in graph["stations"]:
        lat, lon = station["point"]
        features.append(
            {
                "type": "Feature",
                "properties": {"kind": "station", "id": station["id"], "name": station["name"]},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        )
    south, west, north, east = graph["bbox"]
    return {
        "type": "FeatureCollection",
        "features": features,
        "bbox": [west, south, east, north],
        "network_version": NETWORK_VERSION,
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL-1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "approximate": True,
    }


def build_graph() -> dict:
    source_path = ASSET_DIRECTORY / SOURCE_FILE
    metadata = json.loads((ASSET_DIRECTORY / MANIFEST_FILE).read_text())
    metadata["source_file"] = SOURCE_FILE
    source_bytes = source_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != metadata["source_sha256"]:
        raise ValueError("Pinned OSM source checksum differs")
    source = json.loads(gzip.decompress(source_bytes))
    nodes = {e["id"]: e for e in source["elements"] if e["type"] == "node"}
    points = {key: (value["lat"], value["lon"]) for key, value in nodes.items()}
    ways = {e["id"]: e for e in source["elements"] if e["type"] == "way"}
    relations = [e for e in source["elements"] if e["type"] == "relation"]
    south, west, north, east = BBOX

    def covered(node_id: int) -> bool:
        lat, lon = points[node_id]
        return south <= lat <= north and west <= lon <= east

    restrictions = []
    omitted_car_ways = set()
    for relation in relations:
        if relation.get("tags", {}).get("type") != "restriction":
            continue
        tags = relation["tags"]
        if any(value in tags.get("except", "").split(";") for value in ("motorcar", "vehicle")):
            continue
        members = relation["members"]
        from_ids = [m["ref"] for m in members if m["role"] == "from"]
        to_ids = [m["ref"] for m in members if m["role"] == "to"]
        via_ids = [m["ref"] for m in members if m["role"] == "via" and m["type"] == "node"]
        kind = tags.get("restriction:motorcar", tags.get("restriction", ""))
        if (
            len(from_ids) != 1
            or len(to_ids) != 1
            or len(via_ids) != 1
            or any("conditional" in key for key in tags)
            or kind
            not in {
                "no_left_turn",
                "no_right_turn",
                "no_straight_on",
                "no_u_turn",
                "only_left_turn",
                "only_right_turn",
                "only_straight_on",
            }
        ):
            omitted_car_ways.update(from_ids)
            continue
        restrictions.append([from_ids[0], via_ids[0], to_ids[0], kind])
    walk_edges, car_edges, map_roads = [], [], []
    for way_id, way in ways.items():
        tags = way.get("tags", {})
        highway = tags.get("highway")
        if not highway or tags.get("area") == "yes":
            continue
        walk = permitted(tags, "walk") and (
            highway in FOOT_HIGHWAYS or tags.get("foot") in {"yes", "designated"}
        )
        car = permitted(tags, "car") and highway in CAR_HIGHWAYS and way_id not in omitted_car_ways
        if not walk and not car:
            continue
        way_nodes = way["nodes"]
        speed_kmh = 25 if highway not in {"trunk", "primary", "trunk_link"} else 35
        if tags.get("maxspeed", "").isdigit():
            speed_kmh = min(speed_kmh, int(tags["maxspeed"]))
        if speed_kmh <= 0:
            car = False
        for start, finish in zip(way_nodes, way_nodes[1:]):
            if (
                start not in points
                or finish not in points
                or not covered(start)
                or not covered(finish)
            ):
                continue
            length = distance_metres(points[start], points[finish])
            for mode, allowed, edges in (("walk", walk, walk_edges), ("car", car, car_edges)):
                if not allowed or any(
                    not permitted(nodes[node].get("tags", {}), mode) for node in (start, finish)
                ):
                    continue
                if mode == "car" and any(
                    nodes[node].get("tags", {}).get("barrier")
                    in {
                        "bollard",
                        "block",
                        "bus_trap",
                        "cycle_barrier",
                        "stile",
                        "kissing_gate",
                        "gate",
                        "lift_gate",
                        "swing_gate",
                    }
                    for node in (start, finish)
                ):
                    continue
                seconds = length / (WALK_METRES_PER_SECOND if mode == "walk" else speed_kmh / 3.6)
                forward, backward = directions(tags, mode)
                if forward:
                    edges.append([start, finish, round(seconds, 3), way_id])
                if backward:
                    edges.append([finish, start, round(seconds, 3), way_id])
        if highway in {"primary", "trunk"}:
            path = [node for node in way_nodes if node in points and covered(node)]
            if len(path) > 1:
                map_roads.append({"way_id": way_id, "name": tags.get("name", ""), "nodes": path})

    station_nodes = defaultdict(list)
    rail_edges, missing_segments = [], []
    for relation in relations:
        if relation.get("tags", {}).get("route") != "train":
            continue
        stops = [
            m["ref"]
            for m in relation["members"]
            if m["type"] == "node" and m["role"].startswith("stop")
        ]
        adjacency = defaultdict(list)
        for member in relation["members"]:
            way = ways.get(member["ref"]) if member["type"] == "way" else None
            if not way or way.get("tags", {}).get("railway") != "rail":
                continue
            if not permitted(way.get("tags", {}), "car"):
                continue
            for a, b in zip(way["nodes"], way["nodes"][1:]):
                if a not in points or b not in points or not covered(a) or not covered(b):
                    continue
                length = distance_metres(points[a], points[b])
                # The direction of service comes from ordered OSM route stops, not
                # the order in which a mapper happened to draw a bidirectional way.
                forward, backward = directions(way.get("tags", {}), "car")
                if forward:
                    adjacency[a].append((b, length))
                if backward:
                    adjacency[b].append((a, length))
        for stop in stops:
            if stop in nodes and covered(stop):
                name = nodes[stop].get("tags", {}).get("name")
                if name:
                    station_nodes[name].append(stop)
        for start, finish in zip(stops, stops[1:]):
            if (
                start not in points
                or finish not in points
                or not covered(start)
                or not covered(finish)
            ):
                continue
            names = [nodes[node].get("tags", {}).get("name") for node in (start, finish)]
            result = shortest_path(adjacency, start, finish)
            if not all(names) or result is None:
                missing_segments.append([relation["id"], start, finish])
                continue
            length, path = result
            rail_edges.append(
                {
                    "from_name": names[0],
                    "to_name": names[1],
                    "line": relation["tags"]["ref"],
                    "relation_id": relation["id"],
                    "seconds": math.ceil(length / RAIL_METRES_PER_SECOND + 35),
                    "nodes": path,
                }
            )
    walking_nodes = {edge[0] for edge in walk_edges} | {edge[1] for edge in walk_edges}
    walk_links = defaultdict(list)
    for start, finish, _, _ in walk_edges:
        walk_links[start].append(finish)
        walk_links[finish].append(start)
    component_sizes = {}
    unseen = set(walking_nodes)
    while unseen:
        start = unseen.pop()
        component = {start}
        queue = [start]
        while queue:
            for neighbour in walk_links[queue.pop()]:
                if neighbour in unseen:
                    unseen.remove(neighbour)
                    component.add(neighbour)
                    queue.append(neighbour)
        for node in component:
            component_sizes[node] = len(component)
    spatial = defaultdict(list)
    for node in walking_nodes:
        lat, lon = points[node]
        spatial[(int(lat * 500), int(lon * 500))].append(node)
    stations = []
    for name, ids in sorted(station_nodes.items()):
        ids = sorted(set(ids))
        representative = points[ids[0]]
        lat_cell, lon_cell = (int(value * 500) for value in representative)
        nearby = [
            node
            for x in range(lat_cell - 2, lat_cell + 3)
            for y in range(lon_cell - 2, lon_cell + 3)
            for node in spatial.get((x, y), ())
            if component_sizes[node] >= 50
        ]
        nearby.sort(key=lambda node: (distance_metres(points[node], representative), node))
        access_node = nearby[0] if nearby else None
        offset = distance_metres(points[access_node], representative) if nearby else math.inf
        stations.append(
            {
                "id": f"osm-station-{ids[0]}",
                "name": name,
                "stop_nodes": ids,
                "point": list(representative),
                "access_node": access_node if offset <= 180 else None,
                "entry_seconds": math.ceil(offset / WALK_METRES_PER_SECOND + 180)
                if offset <= 180
                else None,
                "access_method": "public-walk-node-within-180m-plus-station-entry-allowance",
            }
        )
    used_nodes = (
        {edge[index] for edges in (walk_edges, car_edges) for edge in edges for index in (0, 1)}
        | {node for edge in rail_edges for node in edge["nodes"]}
        | {node for station in stations for node in station["stop_nodes"]}
        | {node for road in map_roads for node in road["nodes"]}
    )
    graph = {
        "network_version": NETWORK_VERSION,
        "assumptions_version": ASSUMPTIONS_VERSION,
        "nodes": [[node, *points[node]] for node in sorted(used_nodes)],
        "walk_edges": sorted(walk_edges),
        "car_edges": sorted(car_edges),
        "turn_restrictions": sorted(restrictions),
        "stations": stations,
        "rail_edges": rail_edges,
        "map_roads": map_roads,
        "missing_rail_segments": missing_segments,
        "omitted_car_ways": sorted(omitted_car_ways),
        "bbox": BBOX,
    }
    encoded = json.dumps(graph, ensure_ascii=False, separators=(",", ":")).encode()
    compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
    (ASSET_DIRECTORY / GRAPH_FILE).write_bytes(compressed)
    basemap = graph_feature_collection(graph)
    (ASSET_DIRECTORY / BASEMAP_FILE).write_text(
        json.dumps(basemap, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    metadata.update(
        {
            "network_version": NETWORK_VERSION,
            "assumptions_version": ASSUMPTIONS_VERSION,
            "graph_file": GRAPH_FILE,
            "graph_sha256": hashlib.sha256(compressed).hexdigest(),
            "graph_license": "ODbL-1.0",
            "graph_counts": {
                "nodes": len(used_nodes),
                "directed_walk_edges": len(walk_edges),
                "directed_car_edges": len(car_edges),
                "rail_segments": len(rail_edges),
                "stations": len(stations),
                "missing_rail_segments": len(missing_segments),
                "turn_restrictions": len(restrictions),
            },
            "known_missing_rail_segments": [
                {
                    "relation_id": relation,
                    "from": nodes[start].get("tags", {}).get("name"),
                    "to": nodes[finish].get("tags", {}).get("name"),
                }
                for relation, start, finish in missing_segments
            ],
            "assumptions": {
                "walking_m_per_second": WALK_METRES_PER_SECOND,
                "rail_m_per_second": RAIL_METRES_PER_SECOND,
                "rail_stop_allowance_seconds": 35,
                "initial_wait_seconds": 300,
                "transfer_seconds": 480,
                "station_entry_allowance_seconds": 180,
                "station_access_snap_max_metres": 180,
                "road_endpoint_snap_max_metres": 2,
                "car_urban_kmh": 25,
                "car_primary_kmh": 35,
                "car_parking_allowance_seconds": 300,
                "transit_supported_local_minutes": [360, 1320],
                "maximum_walk_to_station_seconds": 2400,
            },
            "limitations": [
                "OSM is incomplete; only downloaded connected topology is used;"
                " no gap-filling edges.",
                "Rail directions follow ordered route-relation stops;"
                " no train timetable or live service.",
                "Station entry is a labelled estimate from a public walk node within 180m,"
                " not a surveyed"
                " entrance, accessible entrance guarantee, or invented road geometry.",
                "Transfers only between retained services at the same named station;"
                " no interstation jumps.",
                "Conditional access and unsupported via-way/conditional turn restrictions"
                " are excluded"
                " conservatively; some possible real car routes are therefore unavailable.",
                "Driving speeds and dwell/wait/parking are conservative assumptions,"
                " not traffic predictions.",
                "Transit is unknown when either commute direction falls outside 06:00–22:00.",
                "Fictional sites and homes use public road nodes;"
                " no actual building occupancy is asserted.",
            ],
            "job_field_sources": [
                {
                    "url": "https://www.hellowork.mhlw.go.jp/enterprise/job_offer01.html",
                    "accessed": "2026-10-06",
                    "use": "求人申込み・紹介・応募手続きの項目参考。求人広告の転載・収集なし。",
                },
                {
                    "url": "https://www.mhlw.go.jp/content/11600000/001188638.pdf",
                    "accessed": "2026-10-06",
                    "sha256": "8789b1eab7aadc09821de101dda8d21076d447f040f1e8f5981312546329d6ea",
                    "use": "業務・就業場所の変更範囲、契約更新基準・更新上限の項目参考。",
                },
            ],
        }
    )
    asset_types = {
        "network": (GRAPH_FILE, "application/gzip"),
        "basemap": (BASEMAP_FILE, "application/geo+json"),
        "manifest": (MANIFEST_FILE, "application/json"),
        "license": (LICENSE_FILE, "text/plain; charset=utf-8"),
    }
    assets = {}
    for key, (filename, media_type) in asset_types.items():
        asset = {
            "filename": filename,
            "url": f"/assets/geography/{filename}",
            "media_type": media_type,
        }
        if filename != MANIFEST_FILE:
            content = (ASSET_DIRECTORY / filename).read_bytes()
            asset.update({"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
        assets[key] = asset
    metadata.update(
        {
            "schema_version": 1,
            "bbox": basemap["bbox"],
            "coverage": {
                "bbox": basemap["bbox"],
                "description": metadata["description"],
                "station_access_radius_m": metadata["station_access_radius_m"],
                "counts": metadata["graph_counts"],
                "known_missing_rail_segments": metadata["known_missing_rail_segments"],
            },
            "public_files": list(PUBLIC_GEOGRAPHY_FILES),
            "assets": assets,
            "source_distribution": (
                "The routing-only sanitized OSM source is retained for offline rebuilding "
                "under source/. Business names, contacts, addresses and contributor metadata "
                "are excluded; only route-stop names and routing tags are retained. "
                "The public ODbL derivative is network.json.gz; it contains no generated records."
            ),
        }
    )
    (ASSET_DIRECTORY / MANIFEST_FILE).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata["graph_counts"], ensure_ascii=False))
    return graph


if __name__ == "__main__":
    build_graph()
