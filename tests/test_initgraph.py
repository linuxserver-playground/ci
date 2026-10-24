from pathlib import Path

from ci.initgraph import build_init_graph, read_units, render_d2, short_image_name

EXPECTED_D2 = """"sonarr:latest": {
  docker-mods
  base {
    fix-attr +\\nlegacy cont-init
  }
  docker-mods -> base
  legacy-services
  custom services
  init-services -> legacy-services
  init-services -> custom services
  custom services -> legacy-services
  legacy-services -> ci-service-check
  init-migrations -> init-adduser
  init-os-end -> init-config
  init-config -> init-config-end
  init-crontab-config -> init-config-end
  init-config -> init-crontab-config
  init-mods-end -> init-custom-files
  init-adduser -> init-device-perms
  base -> init-envfile
  base -> init-migrations
  init-config -> init-mod-sonarr-extra
  init-config-end -> init-mods
  init-mods-package-install -> init-mods-end
  init-mods -> init-mods-package-install
  init-adduser -> init-os-end
  init-device-perms -> init-os-end
  init-envfile -> init-os-end
  init-custom-files -> init-services
  init-config -> init-sonarr-config
  init-services -> svc-cron
  svc-cron -> legacy-services
  init-services -> svc-sonarr
  svc-sonarr -> legacy-services
}
Base Images: {
  "baseimage-alpine:3.22"
}
"sonarr:latest" <- Base Images
"""


def test_read_units(s6_tree: Path) -> None:
    units = {u.name: u for u in read_units(s6_tree)}
    assert "notaunit" not in units and "README" not in units
    assert units["svc-sonarr"].type == "longrun" and units["svc-sonarr"].depends_on == ["init-services"]
    assert units["user"].type == "bundle" and units["user"].contents == ["init-adduser", "svc-sonarr"]
    assert units["init-os-end"].depends_on == ["init-adduser", "init-device-perms", "init-envfile"]
    assert units["init-sonarr-config"].run is not None and "mkdir -p /config" in units["init-sonarr-config"].run
    assert read_units(s6_tree / "missing") == []


def test_render_d2_matches_readme_style(s6_tree: Path) -> None:
    assert render_d2(read_units(s6_tree), "sonarr:latest", ["baseimage-alpine:3.22"]) == EXPECTED_D2


def test_build_init_graph_with_base_comparison(s6_tree: Path) -> None:
    units = read_units(s6_tree)
    base_names = {u.name for u in units if u.name.startswith(("init-", "svc-cron", "ci-")) and "sonarr" not in u.name}
    graph = build_init_graph(units, "sonarr:latest", ["baseimage-alpine:3.22"], base_names)
    records = {u["name"]: u for u in graph["units"]}
    assert graph["d2"] == EXPECTED_D2
    assert records["init-envfile"]["source"] == "base" and "script" not in records["init-envfile"]
    assert records["init-sonarr-config"] == {
        "name": "init-sonarr-config",
        "type": "oneshot",
        "depends_on": ["init-config"],
        "source": "image",
        "script": "#!/usr/bin/with-contenv bash\nmkdir -p /config\n",
    }
    assert records["svc-sonarr"]["source"] == "image" and "exec sonarr" in records["svc-sonarr"]["script"]
    assert records["init-mod-sonarr-extra"]["source"] == "mod" and "script" not in records["init-mod-sonarr-extra"]
    assert records["user"] == {"name": "user", "type": "bundle", "depends_on": [], "source": "image"}


def test_build_init_graph_without_base_uses_known_units(s6_tree: Path) -> None:
    graph = build_init_graph(read_units(s6_tree), "sonarr:latest", ["baseimage-alpine"], None)
    records = {u["name"]: u for u in graph["units"]}
    assert records["svc-cron"]["source"] == "base"
    assert records["user"]["source"] == "base"
    assert records["svc-sonarr"]["source"] == "image"
    assert records["init-mod-sonarr-extra"]["source"] == "mod"


def test_short_image_name() -> None:
    assert short_image_name("ghcr.io/linuxserver/baseimage-alpine:3.22@sha256:abc") == "baseimage-alpine:3.22"
    assert short_image_name("baseimage-ubuntu") == "baseimage-ubuntu"
