"""Offline connected-network commute estimates; no addresses reach an external service."""

import gzip
import hashlib
import heapq
import json
import math
from array import array
from collections import defaultdict
from functools import lru_cache

from backend.saas_geo import (
    ASSUMPTIONS_VERSION as ASSUMPTIONS_VERSION,
)
from backend.saas_geo import (
    BASEMAP_FILE,
    GRAPH_FILE,
    distance_metres,
)
from backend.saas_geo import (
    NETWORK_VERSION as NETWORK_VERSION,
)
from backend.saas_geo import PUBLIC_GEOGRAPHY_FILES as PUBLIC_GEOGRAPHY_FILES
from backend.saas_geo_build import ASSET_DIRECTORY, MANIFEST_FILE
from backend.saas_models import Commute, Job, PersonRecord, Point, StationAccess

MAXIMUM_COMMUTE_SECONDS = 10_800
MAXIMUM_STATION_WALK_SECONDS = 2400
TRANSIT_START_MINUTE = 360
TRANSIT_END_MINUTE = 1320


def commute_allowed(duration_seconds: int | None, personal_limit_minutes: int) -> bool:
    return (
        duration_seconds is not None
        and 0 <= duration_seconds < MAXIMUM_COMMUTE_SECONDS
        and duration_seconds <= personal_limit_minutes * 60
    )


def network_provenance() -> dict:
    """Attribution, pinned checksums, public URLs, coverage, and explicit assumptions."""
    return json.loads((ASSET_DIRECTORY / MANIFEST_FILE).read_text(encoding="utf-8"))


def geography_manifest() -> dict:
    """Schema-versioned metadata for the four allowlisted public geography downloads."""
    return network_provenance()


def public_network_graph() -> dict:
    """The ODbL derived public graph; contains no generated jobs or person records."""
    metadata = network_provenance()
    content = (ASSET_DIRECTORY / GRAPH_FILE).read_bytes()
    if hashlib.sha256(content).hexdigest() != metadata["graph_sha256"]:
        raise ValueError("Pinned public graph checksum differs")
    return json.loads(gzip.decompress(content))


class Network:
    def __init__(self, graph: dict):
        self.version = graph["network_version"]
        self.assumptions_version = graph["assumptions_version"]
        self.nodes = {node: (lat, lon) for node, lat, lon in graph["nodes"]}
        self.node_indexes = {node: index for index, node in enumerate(self.nodes)}
        self.stations = {station["id"]: station for station in graph["stations"]}
        self.station_by_name = {station["name"]: station for station in graph["stations"]}
        self.walk = defaultdict(list)
        self.reverse_walk = defaultdict(list)
        self.car = defaultdict(list)
        self.reverse_car = defaultdict(list)
        for start, finish, seconds, way in graph["walk_edges"]:
            self.walk[start].append((finish, seconds))
            self.reverse_walk[finish].append((start, seconds))
        for start, finish, seconds, way in graph["car_edges"]:
            self.car[start].append((finish, seconds, way))
            self.reverse_car[finish].append((start, seconds, way))
        self.restrictions = defaultdict(list)
        for from_way, via, to_way, kind in graph["turn_restrictions"]:
            self.restrictions[(via, from_way)].append((to_way, kind))
        self.rail = defaultdict(list)
        self.rail_edges = graph["rail_edges"]
        for index, edge in enumerate(self.rail_edges):
            start = self.station_by_name[edge["from_name"]]["id"]
            finish = self.station_by_name[edge["to_name"]]["id"]
            self.rail[start].append((finish, edge["seconds"], edge["line"], index))
        self.map_roads = graph.get("map_roads", [])
        self._locations = defaultdict(list)
        for node, point in self.nodes.items():
            self._locations[(round(point[0], 7), round(point[1], 7))].append(node)
        self._spatial = defaultdict(list)
        for node, (lat, lon) in self.nodes.items():
            self._spatial[(int(lat * 10000), int(lon * 10000))].append(node)

    def road_node(self, point: Point, mode: str) -> int | None:
        adjacency = self.walk if mode == "walk" else self.car
        reverse = self.reverse_walk if mode == "walk" else self.reverse_car
        exact = self._locations.get((round(point.lat, 7), round(point.lon, 7)), ())
        matching = [node for node in exact if node in adjacency or node in reverse]
        if matching:
            return min(matching)
        x, y = int(point.lat * 10000), int(point.lon * 10000)
        candidates = [
            node
            for a in range(x - 1, x + 2)
            for b in range(y - 1, y + 2)
            for node in self._spatial.get((a, b), ())
            if node in adjacency or node in reverse
        ]
        if not candidates:
            return None
        nearest = min(
            candidates, key=lambda node: distance_metres((point.lat, point.lon), self.nodes[node])
        )
        return (
            nearest if distance_metres((point.lat, point.lon), self.nodes[nearest]) <= 2 else None
        )

    @lru_cache(maxsize=160)
    def station_walks(self, station_id: str, *, towards_station: bool = False) -> dict[int, float]:
        station = self.stations.get(station_id)
        if station is None or station["access_node"] is None:
            return {}
        start = station["access_node"]
        adjacency = self.reverse_walk if towards_station else self.walk
        distances = {start: 0.0}
        queue = [(0.0, start)]
        while queue:
            cost, node = heapq.heappop(queue)
            if cost != distances[node]:
                continue
            for neighbour, seconds in adjacency.get(node, ()):
                candidate = cost + seconds
                if candidate <= MAXIMUM_STATION_WALK_SECONDS and candidate < distances.get(
                    neighbour, math.inf
                ):
                    distances[neighbour] = candidate
                    heapq.heappush(queue, (candidate, neighbour))
        return distances

    def station_options(self, node: int, *, limit: int = 3) -> list[StationAccess]:
        candidates = []
        for station_id, station in self.stations.items():
            outward = self.station_walks(station_id).get(node)
            inward = self.station_walks(station_id, towards_station=True).get(node)
            if outward is not None and inward is not None:
                seconds = max(outward, inward) + station["entry_seconds"]
                candidates.append((seconds, station_id))
        return [
            StationAccess(
                station_id=station_id,
                name=self.stations[station_id]["name"],
                walking_minutes=math.ceil(seconds / 60),
            )
            for seconds, station_id in sorted(candidates)[:limit]
        ]

    @lru_cache(maxsize=8192)
    def rail_route(self, start: str, finish: str) -> tuple[int, tuple[int, ...]] | None:
        if start not in self.stations or finish not in self.stations:
            return None
        if start == finish:
            return 0, ()
        initial = (start, "")
        queue = [(0, initial)]
        distances = {initial: 0}
        previous = {}
        while queue:
            cost, state = heapq.heappop(queue)
            if cost != distances[state]:
                continue
            station_id, current_line = state
            if station_id == finish:
                edges = []
                while state != initial:
                    state, edge_index = previous[state]
                    edges.append(edge_index)
                return cost, tuple(reversed(edges))
            for neighbour, seconds, line, edge_index in self.rail.get(station_id, ()):
                waiting = 300 if not current_line else (480 if current_line != line else 0)
                candidate = cost + seconds + waiting
                next_state = (neighbour, line)
                if candidate < distances.get(next_state, math.inf):
                    distances[next_state] = candidate
                    previous[next_state] = state, edge_index
                    heapq.heappush(queue, (candidate, next_state))
        return None

    def _turn_permitted(
        self, node: int, incoming: int, outgoing: int, previous_node: int, next_node: int
    ) -> bool:
        if not outgoing:
            return True
        for to_way, kind in self.restrictions.get((node, incoming), ()):
            if kind == "no_u_turn":
                if to_way == outgoing and previous_node == next_node:
                    return False
            elif kind.startswith("no_") and to_way == outgoing:
                return False
            elif kind.startswith("only_") and to_way != outgoing:
                return False
        return True

    @lru_cache(maxsize=64)
    def car_distances_to(self, finish: int, *, reverse: bool = True) -> array:
        """One turn-aware search per site/direction; retain a compact 8-byte/node result."""
        best = array("d", [math.inf]) * len(self.nodes)
        initial = (finish, 0, 0)
        distances = {initial: 0.0}
        queue = [(0.0, initial)]
        while queue:
            cost, state = heapq.heappop(queue)
            if cost != distances[state]:
                continue
            node, outgoing_way, next_node = state
            index = self.node_indexes[node]
            if cost < best[index]:
                best[index] = cost
            adjacency = self.reverse_car if reverse else self.car
            for previous_node, seconds, incoming_way in adjacency.get(node, ()):
                legal = (
                    self._turn_permitted(node, incoming_way, outgoing_way, previous_node, next_node)
                    if reverse
                    else self._turn_permitted(
                        node, outgoing_way, incoming_way, next_node, previous_node
                    )
                )
                if not legal:
                    continue
                previous_state = (previous_node, incoming_way, node)
                candidate = cost + seconds
                if candidate < distances.get(previous_state, math.inf):
                    distances[previous_state] = candidate
                    heapq.heappush(queue, (candidate, previous_state))
        return best

    def car_seconds(self, start: int, finish: int, *, returning: bool = False) -> int | None:
        seconds = self.car_distances_to(finish, reverse=not returning)[self.node_indexes[start]]
        return math.ceil(seconds + 300) if math.isfinite(seconds) else None

    def _transit(
        self, person: PersonRecord, job: Job
    ) -> tuple[int, tuple[int, ...], list[str]] | None:
        home = self.road_node(person.private_location, "walk")
        site = self.road_node(job.site.point, "walk")
        if home is None or site is None:
            return None
        best = None
        for origin in person.stations:
            first = self.stations.get(origin.station_id)
            if first is None:
                continue
            access = self.station_walks(origin.station_id, towards_station=True).get(home)
            return_egress = self.station_walks(origin.station_id).get(home)
            if access is None or return_egress is None:
                continue
            for destination in job.site.stations:
                last = self.stations.get(destination.station_id)
                if last is None:
                    continue
                egress = self.station_walks(destination.station_id).get(site)
                return_access = self.station_walks(
                    destination.station_id, towards_station=True
                ).get(site)
                if egress is None or return_access is None:
                    continue
                outward = self.rail_route(origin.station_id, destination.station_id)
                returning = self.rail_route(destination.station_id, origin.station_id)
                if outward is None or returning is None:
                    continue
                station_entry = first["entry_seconds"] + last["entry_seconds"] if outward[1] else 0
                # Both directions must exist. Return travel can be slower on one-way roads/rails.
                seconds = math.ceil(
                    max(
                        access + outward[0] + egress + station_entry,
                        return_access + returning[0] + return_egress + station_entry,
                    )
                )
                if best is None or seconds < best[0]:
                    names = [first["name"]]
                    names.extend(self.rail_edges[index]["to_name"] for index in outward[1])
                    best = seconds, outward[1], names
        return best

    def estimate(self, person: PersonRecord, job: Job) -> Commute:
        options = []
        unknown = []
        outside_hours = False
        if "transit" in person.commute_modes:
            result = self._transit(person, job)
            if result is None:
                unknown.append("収録済み徒歩・鉄道ネットワークに往復の接続がありません")
            else:
                seconds, edge_indexes, names = result
                start = job.schedule.start_minute * 60
                end = job.schedule.end_minute * 60
                outside_hours = (
                    start - seconds < TRANSIT_START_MINUTE * 60
                    or end + seconds > TRANSIT_END_MINUTE * 60
                )
                if outside_hours:
                    unknown.append("06:00〜22:00の概算対応時間外。夜間の実ダイヤは未収録です")
                else:
                    paths = [[]]
                    for index in edge_indexes:
                        edge = self.rail_edges[index]
                        if paths[-1] and paths[-1][-1] != edge["nodes"][0]:
                            paths.append([])
                        for node in self.rail_edges[index]["nodes"]:
                            if not paths[-1] or node != paths[-1][-1]:
                                paths[-1].append(node)
                    route_nodes = max(paths, key=len)
                    public_path = [
                        Point(lat=self.nodes[node][0], lon=self.nodes[node][1])
                        for node in route_nodes
                    ]
                    options.append(
                        Commute(
                            status="estimated",
                            mode="transit",
                            duration_seconds=seconds,
                            eligible=commute_allowed(seconds, person.max_commute_minutes),
                            summary="接続済み道路・鉄道から往復の遅い方向を概算。待ち・乗換え・駅構内歩行込み。",
                            station_names=names,
                            public_path=public_path,
                            network_version=self.version,
                            assumptions_version=self.assumptions_version,
                            uncertainty=(
                                "概算。時刻表・運行状況は未反映。経路線は最長の連続鉄道区間だけを表示し、"
                                "自宅側の道路・駅構内乗換え接続は表示しません。"
                            ),
                        )
                    )
        if "car" in person.commute_modes and person.driving_eligible and job.site.parking:
            home = self.road_node(person.private_location, "car")
            site = self.road_node(job.site.point, "car")
            if home is None or site is None:
                unknown.append("自動車で接続できる収録道路上の出発点・到着点を確認できません")
            else:
                outward = self.car_seconds(home, site)
                returning = self.car_seconds(home, site, returning=True)
                if outward is None or returning is None:
                    unknown.append("通行方向・アクセス制限を満たす往復の道路接続がありません")
                else:
                    seconds = max(outward, returning)
                    options.append(
                        Commute(
                            status="estimated",
                            mode="car",
                            duration_seconds=seconds,
                            eligible=commute_allowed(seconds, person.max_commute_minutes),
                            summary="道路接続・一方通行・収録右左折制限を確認。往復の遅い方向と駐車5分を概算。",
                            network_version=self.version,
                            assumptions_version=self.assumptions_version,
                        )
                    )
        if options:
            return min(options, key=lambda option: option.duration_seconds)
        if unknown:
            return Commute(
                status="unknown",
                mode=None,
                eligible=False,
                summary="。".join(unknown),
                network_version=self.version,
                assumptions_version=self.assumptions_version,
            )
        return Commute(
            status="unreachable",
            mode=None,
            eligible=False,
            summary="利用可能な通勤手段がありません。自動車には運転資格と勤務先駐車場が必要です。",
            network_version=self.version,
            assumptions_version=self.assumptions_version,
        )


@lru_cache(maxsize=1)
def load_network() -> Network:
    return Network(public_network_graph())


def estimate_commute(person: PersonRecord, job: Job) -> Commute:
    return load_network().estimate(person, job)


@lru_cache(maxsize=1)
def public_feature_collection() -> dict:
    """Same-origin approximate base map: public road/rail geometry and station points only."""
    metadata = network_provenance()
    content = (ASSET_DIRECTORY / BASEMAP_FILE).read_bytes()
    if hashlib.sha256(content).hexdigest() != metadata["assets"]["basemap"]["sha256"]:
        raise ValueError("Pinned public basemap checksum differs")
    return json.loads(content)
