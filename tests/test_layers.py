from typing import Any
from unittest.mock import MagicMock

from ci.layers import base_image_ref, build_layers, clean_created_by, find_base_image, match_history
from tests.conftest import LAYER_A, LAYER_B, LAYER_C


def test_match_history_by_size(history_records: list[dict[str, Any]]) -> None:
    matched = match_history(history_records, 3)
    assert [m["CreatedBy"].split(" ")[0] if m else None for m in matched] == ["COPY", "RUN", "COPY"]
    assert matched[0] is not None and matched[0]["Size"] == 15000000


def test_match_history_falls_back_to_instructions(history_records: list[dict[str, Any]]) -> None:
    history_records[1]["Size"] = 0  # an empty COPY still produced a layer
    matched = match_history(history_records, 3)
    assert matched[2] is not None and matched[2]["CreatedBy"].startswith("COPY root/")


def test_match_history_gives_up_when_unreconcilable(history_records: list[dict[str, Any]]) -> None:
    assert match_history(history_records, 9) == [None] * 9


def test_clean_created_by() -> None:
    assert clean_created_by("COPY root/ / # buildkit") == "COPY root/ /"
    assert clean_created_by('/bin/sh -c #(nop)  CMD ["bash"]') == 'CMD ["bash"]'
    assert clean_created_by("/bin/sh -c apk add bash") == "RUN apk add bash"


def test_build_layers(
    inspect_record: dict[str, Any], history_records: list[dict[str, Any]], base_inspect_record: dict[str, Any]
) -> None:
    manifest_layers = [
        {"digest": "sha256:" + "d" * 64, "size": 6000000},
        {"digest": "sha256:" + "e" * 64, "size": 100000},
        {"digest": "sha256:" + "f" * 64, "size": 2000},
    ]
    layers = build_layers(inspect_record, history_records, base_inspect_record, manifest_layers)
    assert [layer["index"] for layer in layers] == [0, 1, 2]
    assert layers[0] == {
        "index": 0,
        "diff_id": LAYER_A,
        "uncompressed_size": 15000000,
        "digest": "sha256:" + "d" * 64,
        "size": 6000000,
        "created_by": "COPY /root-out/ /",
        "from_base": True,
        "empty": False,
    }
    assert layers[1]["from_base"] is True and layers[1]["created_by"].startswith("RUN /bin/sh -c echo")
    assert layers[2]["from_base"] is False and layers[2]["created_by"] == "COPY root/ /"
    assert layers[2]["diff_id"] == LAYER_C


def test_build_layers_without_registry_or_base(
    inspect_record: dict[str, Any], history_records: list[dict[str, Any]]
) -> None:
    layers = build_layers(inspect_record, history_records, None, None)
    assert all("from_base" not in layer for layer in layers)
    assert layers[0]["digest"] == LAYER_A and layers[0]["size"] == 15000000
    layers = build_layers(inspect_record, history_records, None, [{"digest": "sha256:" + "0" * 64, "size": 1}])
    assert layers[0]["digest"] == LAYER_A  # mismatched manifest is ignored


def test_base_that_is_not_a_prefix_is_ignored(
    inspect_record: dict[str, Any], history_records: list[dict[str, Any]]
) -> None:
    other = {"RootFS": {"Layers": [LAYER_B]}}
    assert all("from_base" not in layer for layer in build_layers(inspect_record, history_records, other))


def test_base_image_ref() -> None:
    labels = {"io.linuxserver.image.base": "ghcr.io/linuxserver/baseimage-alpine:3.22@sha256:" + "f" * 64}
    assert base_image_ref(labels, "alpine") == "ghcr.io/linuxserver/baseimage-alpine:3.22"
    assert base_image_ref({}, "ubuntu") == "ghcr.io/linuxserver/baseimage-ubuntu"
    assert base_image_ref({}, "scratch") is None


def test_find_base_image_prefers_longest_prefix(
    inspect_record: dict[str, Any], base_inspect_record: dict[str, Any]
) -> None:
    client = MagicMock()
    short = {"Id": "sha256:" + "3" * 64, "RootFS": {"Layers": [LAYER_A]}}
    images = {
        "ghcr.io/linuxserver/baseimage-alpine:3.22": base_inspect_record,
        "ghcr.io/linuxserver/baseimage-alpine:3.21": short,
        "ghcr.io/linuxserver-playground/test:latest": inspect_record,
    }

    def get(ref: str) -> MagicMock:
        image = MagicMock()
        image.attrs = images[ref]
        image.id = images[ref]["Id"]
        return image

    client.images.get.side_effect = get
    client.images.list.return_value = [
        MagicMock(attrs={"RepoTags": [ref]}, id=attrs["Id"]) for ref, attrs in images.items()
    ]
    found = find_base_image(client, inspect_record, inspect_record["Config"]["Labels"], "alpine")
    assert found is not None
    assert found[0] == "ghcr.io/linuxserver/baseimage-alpine:3.22"
    assert find_base_image(client, short, {}, "alpine") is None
