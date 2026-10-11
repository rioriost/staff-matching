import gzip
import hashlib
import json
import math

import pytest

from backend.saas_data import generate_tenant
from backend.saas_geo import directions, permitted
from backend.saas_geo_build import ASSET_DIRECTORY
from backend.saas_models import Point, Schedule, StationAccess
from backend.saas_routing import (
    ASSUMPTIONS_VERSION,
    NETWORK_VERSION,
    PUBLIC_GEOGRAPHY_FILES,
    Network,
    commute_allowed,
    estimate_commute,
    geography_manifest,
    load_network,
    network_provenance,
    public_feature_collection,
    public_network_graph,
)


@pytest.mark.parametrize(
    "seconds,maximum,allowed",
    [
        (10799, 180, True),
        (10800, 180, False),
        (10801, 180, False),
        (10799, 179, False),
        (10740, 179, True),
        (10741, 179, False),
        (3600, 60, True),
        (3601, 60, False),
        (0, 30, True),
        (None, 180, False),
        (-1, 180, False),
        (10800, 240, False),
    ],
)
def test_strict_three_hour_and_inclusive_personal_limits(seconds, maximum, allowed):
    assert commute_allowed(seconds, maximum) is allowed


@pytest.fixture
def graph():
    # Deliberately tiny test-only topology: no fabricated edges enter production assets.
    return {
        "network_version": "test-connected",
        "assumptions_version": "test-assumptions",
        "nodes": [[n, 35.6 + n / 10000, 139.7 + n / 10000] for n in range(1, 13)],
        "walk_edges": [
            [1, 2, 60, 1],
            [2, 1, 60, 1],
            [3, 4, 60, 2],
            [4, 3, 60, 2],
            [4, 5, 600, 3],
            [5, 4, 600, 3],
        ],
        "car_edges": [
            [1, 2, 100, 10],
            [2, 1, 100, 10],
            [2, 3, 100, 20],
            [3, 2, 100, 20],
            [3, 4, 100, 30],
            [4, 3, 100, 30],
        ],
        "stations": [
            {
                "id": "A",
                "name": "駅A",
                "access_node": 2,
                "entry_seconds": 180,
                "point": [35.601, 139.701],
            },
            {
                "id": "B",
                "name": "駅B",
                "access_node": 3,
                "entry_seconds": 180,
                "point": [35.6011, 139.7011],
            },
            {
                "id": "C",
                "name": "駅C",
                "access_node": 5,
                "entry_seconds": 180,
                "point": [35.6012, 139.7012],
            },
            {
                "id": "D",
                "name": "駅D",
                "access_node": None,
                "entry_seconds": None,
                "point": [35.6009, 139.7009],
            },
        ],
        "rail_edges": [
            {
                "from_name": "駅A",
                "to_name": "駅B",
                "seconds": 300,
                "line": "L1",
                "nodes": [10, 11],
                "relation_id": 100,
            },
            {
                "from_name": "駅B",
                "to_name": "駅A",
                "seconds": 300,
                "line": "L1",
                "nodes": [11, 10],
                "relation_id": 101,
            },
        ],
        "turn_restrictions": [],
    }


@pytest.fixture
def records():
    dataset = generate_tenant("demo", count=1)
    person = dataset.people[0].model_copy(deep=True)
    job = dataset.jobs[0].model_copy(deep=True)
    person.private_location = Point(lat=35.6001, lon=139.7001)
    job.site.point = Point(lat=35.6004, lon=139.7004)
    person.stations = [StationAccess(station_id="A", name="駅A", walking_minutes=1)]
    job.site.stations = [StationAccess(station_id="B", name="駅B", walking_minutes=1)]
    job.schedule = Schedule(weekdays=[0, 1, 2, 3, 4], start_minute=540, end_minute=1020)
    job.site.parking = True
    person.commute_modes = ["transit"]
    person.driving_eligible = True
    person.max_commute_minutes = 180
    return person, job


def test_connected_transit_uses_walking_waiting_and_station_geometry(graph, records):
    person, job = records
    result = Network(graph).estimate(person, job)
    assert result.status == "estimated"
    assert result.duration_seconds == 1080  # 2*60 walk + 2*180 entry + 300 wait + 300 rail
    assert result.eligible
    assert result.station_names == ["駅A", "駅B"]
    assert len(result.public_path) == 2
    assert person.private_location not in result.public_path
    assert job.site.point not in result.public_path


def test_disconnected_rail_and_outside_coverage_are_unknown_not_straight_lines(graph, records):
    person, job = records
    graph["rail_edges"] = []
    network = Network(graph)
    result = network.estimate(person, job)
    assert result.status == "unknown" and not result.eligible
    assert result.duration_seconds is None and result.public_path == []
    person.private_location = Point(lat=0, lon=0)
    assert network.estimate(person, job).status == "unknown"


def test_near_but_disconnected_roads_do_not_create_a_route(graph, records):
    person, job = records
    person.private_location = Point(lat=35.6006, lon=139.7006)
    assert Network(graph).estimate(person, job).status == "unknown"


def test_all_station_alternatives_are_considered_not_only_first(graph, records):
    person, job = records
    job.site.stations.insert(0, StationAccess(station_id="C", name="駅C", walking_minutes=1))
    person.stations.insert(0, StationAccess(station_id="missing", name="不明", walking_minutes=0))
    result = Network(graph).estimate(person, job)
    assert result.eligible
    assert result.station_names[-1] == "駅B"
    assert result.duration_seconds == 1080


def test_train_transfer_requires_same_station_and_charges_wait(graph):
    graph["rail_edges"] = [
        {
            "from_name": "駅A",
            "to_name": "駅D",
            "seconds": 200,
            "line": "L1",
            "nodes": [10, 9],
            "relation_id": 100,
        },
        {
            "from_name": "駅D",
            "to_name": "駅B",
            "seconds": 300,
            "line": "L2",
            "nodes": [9, 11],
            "relation_id": 200,
        },
    ]
    network = Network(graph)
    assert network.rail_route("A", "B")[0] == 200 + 300 + 300 + 480
    assert network.rail_route("B", "A") is None
    assert network.rail_route("A", "C") is None


def test_missing_return_direction_is_not_eligible(graph, records):
    person, job = records
    graph["rail_edges"] = graph["rail_edges"][:1]
    result = Network(graph).estimate(person, job)
    assert result.status == "unknown"
    assert not result.eligible


@pytest.mark.parametrize("start,end", [(300, 840), (1200, 1740), (900, 1320)])
def test_unsupported_transit_hours_are_unknown(graph, records, start, end):
    person, job = records
    job.schedule = Schedule(weekdays=[0, 1], start_minute=start, end_minute=end)
    result = Network(graph).estimate(person, job)
    assert result.status == "unknown" and not result.eligible
    assert "06:00" in result.summary


def test_car_requires_eligibility_and_parking_and_hides_origin_geometry(graph, records):
    person, job = records
    person.commute_modes = ["car"]
    network = Network(graph)
    result = network.estimate(person, job)
    assert result.mode == "car" and result.duration_seconds == 600 and result.eligible
    assert result.public_path == []
    person.driving_eligible = False
    assert network.estimate(person, job).status == "unreachable"
    person.driving_eligible = True
    job.site.parking = False
    assert network.estimate(person, job).status == "unreachable"


def test_night_shift_can_use_an_allowed_car_but_not_invented_train(graph, records):
    person, job = records
    person.commute_modes = ["transit", "car"]
    job.schedule = Schedule(weekdays=[0, 1], start_minute=1200, end_minute=1740)
    result = Network(graph).estimate(person, job)
    assert result.mode == "car" and result.eligible


def test_directed_car_edges_and_turn_restrictions_are_enforced(graph, records):
    person, job = records
    person.commute_modes = ["car"]
    graph["turn_restrictions"] = [[10, 2, 20, "no_right_turn"]]
    network = Network(graph)
    assert network.car_seconds(1, 4) is None
    assert network.estimate(person, job).status == "unknown"
    graph["turn_restrictions"] = []
    graph["car_edges"] = [edge for edge in graph["car_edges"] if edge[0] < edge[1]]
    network = Network(graph)
    assert network.car_seconds(1, 4) == 600
    assert network.car_seconds(1, 4, returning=True) is None
    assert network.estimate(person, job).status == "unknown"


def test_no_uturn_does_not_forbid_continuing_on_the_same_way(graph):
    graph["turn_restrictions"] = [[10, 2, 10, "no_u_turn"]]
    network = Network(graph)
    assert network._turn_permitted(2, 10, 10, 1, 3)
    assert not network._turn_permitted(2, 10, 10, 1, 1)


@pytest.mark.parametrize(
    "tags,mode,expected",
    [
        ({"access": "private"}, "walk", False),
        ({"access": "private", "foot": "yes"}, "walk", True),
        ({"motor_vehicle": "no"}, "car", False),
        ({"motorcar": "private", "access": "yes"}, "car", False),
        ({"foot": "no"}, "walk", False),
        ({"access:conditional": "no @ (22:00-06:00)"}, "car", False),
        ({"oneway:conditional": "yes @ (08:00-10:00)"}, "car", False),
        ({"oneway": "reversible"}, "car", False),
    ],
)
def test_public_access_rules_fail_closed_for_unsupported_conditions(tags, mode, expected):
    assert permitted(tags, mode) is expected


def test_directional_access_is_mode_specific():
    assert directions({"oneway": "yes"}, "car") == (True, False)
    assert directions({"oneway": "-1"}, "car") == (False, True)
    assert directions({"oneway": "yes"}, "walk") == (True, True)
    assert directions({"oneway:foot": "-1"}, "walk") == (False, True)


def test_pinned_assets_attribution_versions_and_actual_geometry():
    metadata = network_provenance()
    assert metadata["license"] == metadata["graph_license"] == "ODbL-1.0"
    assert metadata["network_version"] == NETWORK_VERSION
    assert metadata["assumptions_version"] == ASSUMPTIONS_VERSION
    for filename, checksum in (
        (metadata["source_file"], metadata["source_sha256"]),
        (metadata["graph_file"], metadata["graph_sha256"]),
    ):
        content = (ASSET_DIRECTORY / filename).read_bytes()
        assert hashlib.sha256(content).hexdigest() == checksum
        assert len(gzip.decompress(content)) > 100000
    assert metadata["graph_counts"]["nodes"] > 100000
    assert metadata["graph_counts"]["turn_restrictions"] > 500
    assert metadata["known_missing_rail_segments"]
    graph = public_network_graph()
    road_pairs = {(edge[0], edge[1]) for edge in graph["walk_edges"]}
    assert len(road_pairs) < len(graph["nodes"]) * 4
    assert all(
        edge["relation_id"] in metadata["route_relation_ids"] for edge in graph["rail_edges"]
    )
    assert max(len(edge["nodes"]) for edge in graph["rail_edges"]) > 20
    assert load_network() is load_network()


def test_every_derived_road_and_rail_edge_has_real_osm_way_continuity():
    metadata = network_provenance()
    source = json.loads(gzip.decompress((ASSET_DIRECTORY / metadata["source_file"]).read_bytes()))
    ways = {element["id"]: element for element in source["elements"] if element["type"] == "way"}
    source_edges = {
        way_id: {(start, finish) for start, finish in zip(way["nodes"], way["nodes"][1:])}
        for way_id, way in ways.items()
    }
    graph = public_network_graph()
    for mode, edges in (("walk", graph["walk_edges"]), ("car", graph["car_edges"])):
        for start, finish, _, way_id in edges:
            forward, backward = directions(ways[way_id].get("tags", {}), mode)
            assert (
                forward
                and (start, finish) in source_edges[way_id]
                or backward
                and (finish, start) in source_edges[way_id]
            )
            assert permitted(ways[way_id].get("tags", {}), mode)
    relation_ways = {
        element["id"]: {member["ref"] for member in element["members"] if member["type"] == "way"}
        for element in source["elements"]
        if element["type"] == "relation" and element.get("tags", {}).get("route") == "train"
    }
    for rail in graph["rail_edges"]:
        real_pairs = {
            pair
            for way_id in relation_ways[rail["relation_id"]]
            if way_id in ways and ways[way_id].get("tags", {}).get("railway") == "rail"
            for pair in source_edges[way_id]
        }
        for start, finish in zip(rail["nodes"], rail["nodes"][1:]):
            assert (start, finish) in real_pairs or (finish, start) in real_pairs


def test_public_basemap_never_contains_person_records():
    collection = public_feature_collection()
    assert collection["type"] == "FeatureCollection"
    assert collection["approximate"]
    assert "OpenStreetMap" in collection["attribution"]
    assert {feature["properties"]["kind"] for feature in collection["features"]} == {
        "rail",
        "road",
        "station",
    }
    assert all("person" not in str(feature["properties"]) for feature in collection["features"])


def test_four_standardized_public_assets_and_manifest_contract():
    manifest = geography_manifest()
    assert manifest == network_provenance()
    assert manifest["schema_version"] == 1
    assert PUBLIC_GEOGRAPHY_FILES == (
        "network.json.gz",
        "basemap.geojson",
        "manifest.json",
        "LICENSE.txt",
    )
    assert manifest["public_files"] == list(PUBLIC_GEOGRAPHY_FILES)
    assert set(manifest["assets"]) == {"network", "basemap", "manifest", "license"}
    assert manifest["bbox"] == [139.55, 35.43, 140.04, 35.92]
    assert manifest["coverage"]["bbox"] == manifest["bbox"]
    assert manifest["coverage"]["counts"] == manifest["graph_counts"]
    for asset in manifest["assets"].values():
        content = (ASSET_DIRECTORY / asset["filename"]).read_bytes()
        assert asset["url"] == "/assets/geography/" + asset["filename"]
        if asset["filename"] != "manifest.json":
            assert asset["bytes"] == len(content)
            assert asset["sha256"] == hashlib.sha256(content).hexdigest()
    assert manifest["source_file"].startswith("source/")
    assert (
        json.loads((ASSET_DIRECTORY / "basemap.geojson").read_text(encoding="utf-8"))
        == public_feature_collection()
    )
    assert not {"people", "companies", "jobs", "sites"}.intersection(public_network_graph())
    assert "OpenStreetMap contributors" in (ASSET_DIRECTORY / "LICENSE.txt").read_text()


def test_real_corridor_route_has_public_rail_path_and_eligible_candidates():
    data = generate_tenant("demo", count=80)
    results = [
        estimate_commute(person, data.jobs[(index * 7 + 3) % len(data.jobs)])
        for index, person in enumerate(data.people)
    ]
    assert any(result.eligible for result in results)
    assert any(result.status == "unknown" for result in results)
    routed = [(index, result) for index, result in enumerate(results) if result.public_path]
    assert routed
    for index, result in routed:
        assert data.people[index].private_location not in result.public_path
        assert result.mode == "transit"
        assert math.isfinite(result.duration_seconds)
