import gzip
import json
from io import BytesIO

from backend import saas_geo_build
from backend.saas_geo_build import (
    ASSET_DIRECTORY,
    ROUTE_IDS,
    SOURCE_FILE,
    SOURCE_PRIVACY_POLICY,
    sanitize_source,
)


def test_source_sanitization_removes_business_contacts_and_contributor_metadata():
    source = {
        "osm3s": {"timestamp_osm_base": "2026-10-06T03:09:03Z", "note": "PRIVATE-MARKER"},
        "elements": [
            {
                "type": "node", "id": 1, "lat": 35.6, "lon": 139.7,
                "user": "PRIVATE-MARKER", "uid": 123,
                "tags": {
                    "name": "駅A", "operator": "PRIVATE-MARKER", "brand": "PRIVATE-MARKER",
                    "contact:phone": "PRIVATE-MARKER", "addr:housenumber": "PRIVATE-MARKER",
                    "note": "PRIVATE-MARKER", "barrier": "gate", "access": "private",
                },
            },
            {
                "type": "node", "id": 2, "lat": 35.61, "lon": 139.71,
                "tags": {"name": "PRIVATE-MARKER", "phone": "PRIVATE-MARKER"},
            },
            {
                "type": "way", "id": 3, "nodes": [1, 2],
                "tags": {
                    "name": "PRIVATE-MARKER", "highway": "primary", "maxspeed": "30",
                    "oneway:motorcar": "-1", "foot:conditional": "no @ (22:00-06:00)",
                    "motor_vehicle:conditional": "no @ (22:00-06:00)",
                },
            },
            {
                "type": "relation", "id": ROUTE_IDS[0],
                "tags": {"route": "train", "ref": "JK", "operator": "PRIVATE-MARKER"},
                "members": [
                    {"type": "node", "ref": 1, "role": "stop", "name": "PRIVATE-MARKER"},
                    {"type": "way", "ref": 3, "role": ""},
                ],
            },
            {
                "type": "relation", "id": 4, "members": [],
                "tags": {
                    "type": "restriction", "restriction": "no_right_turn",
                    "restriction:hgv:conditional": "no_left_turn @ (08:00-10:00)",
                },
            },
        ],
    }
    clean = sanitize_source(source)
    assert "PRIVATE-MARKER" not in json.dumps(clean)
    assert clean["elements"][0]["tags"] == {
        "name": "駅A", "barrier": "gate", "access": "private",
    }
    assert "tags" not in clean["elements"][1]
    assert clean["elements"][2]["tags"] == {
        key: value for key, value in source["elements"][2]["tags"].items() if key != "name"
    }
    assert clean["elements"][3]["tags"] == {"route": "train", "ref": "JK"}
    assert clean["elements"][4]["tags"] == source["elements"][4]["tags"]
    assert sanitize_source(clean) == clean
    assert source["elements"][0]["user"] == "PRIVATE-MARKER"


def test_pinned_source_is_sanitized_and_names_are_only_route_stops():
    manifest = json.loads((ASSET_DIRECTORY / "manifest.json").read_text())
    assert manifest["source_privacy_policy"] == SOURCE_PRIVACY_POLICY
    source = json.loads(gzip.decompress((ASSET_DIRECTORY / SOURCE_FILE).read_bytes()))
    assert sanitize_source(source) == source
    stops = {
        member["ref"]
        for element in source["elements"]
        if element["type"] == "relation" and element.get("tags", {}).get("route") == "train"
        for member in element["members"]
        if member["type"] == "node" and member["role"].startswith("stop")
    }
    for element in source["elements"]:
        assert not {"user", "uid", "changeset", "timestamp"}.intersection(element)
        tags = element.get("tags", {})
        assert not any(
            key.startswith(("operator", "brand", "contact", "addr:"))
            or key in {"phone", "website", "email", "note", "description"}
            for key in tags
        )
        if "name" in tags:
            assert element["type"] == "node" and element["id"] in stops


def test_download_only_persists_sanitized_source(tmp_path, monkeypatch):
    source = {
        "osm3s": {"timestamp_osm_base": "2026-10-06T03:09:03Z"},
        "elements": [{
            "type": "node", "id": 1, "lat": 35.6, "lon": 139.7,
            "user": "PRIVATE-MARKER",
            "tags": {"name": "PRIVATE-MARKER", "operator": "PRIVATE-MARKER"},
        }],
    }
    monkeypatch.setattr(saas_geo_build, "ASSET_DIRECTORY", tmp_path)
    monkeypatch.setattr(saas_geo_build, "source_queries", lambda: ["test query"])
    monkeypatch.setattr(
        saas_geo_build, "urlopen", lambda *args, **kwargs: BytesIO(json.dumps(source).encode())
    )
    saas_geo_build.download()
    result = gzip.decompress((tmp_path / SOURCE_FILE).read_bytes()).decode()
    assert "PRIVATE-MARKER" not in result
    assert json.loads(result) == sanitize_source(source)
    assert not list(tmp_path.glob("osm-source-part-*.json.gz"))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["source_privacy_policy"] == SOURCE_PRIVACY_POLICY
