# ci

The LinuxServer v2 container test harness. The shared GitHub Actions workflow runs it once per
architecture, on a native runner, against an image that is already in the local Docker daemon. It starts
the container, watches it come up, inspects it, generates the bundle assets and writes everything into one
output directory that the build attaches to the release (see `CONVENTIONS.md` §6 in
[linuxserver-playground/builder](https://github.com/linuxserver-playground/builder)).

It is a rewrite of `linuxserver/docker-ci` for the playground: one image, one tag, one architecture per run;
no S3, no Jenkins, no Docker Hub. GPL-3.0, as before.

## Interface

```
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$OUT":/out \
  -e IMAGE="<image ref present in the local docker daemon>" \
  -e ARCH="amd64|arm64" \
  -e MODE="live|dev|pr" \
  -e MANIFEST_JSON="<image.yml as compact JSON>" \
  -e PREVIOUS_PACKAGES_URL="<optional URL of the previous release's package_versions.<arch>.txt>" \
  ghcr.io/linuxserver-playground/ci:latest
```

| Variable | Required | Meaning |
| --- | --- | --- |
| `IMAGE` | yes | Reference of the image under test. It must already be in the daemon; nothing is pulled. |
| `ARCH` | yes | `amd64` or `arm64`. Only names the output files; the runner must be native. |
| `MODE` | yes | `live`, `dev` or `pr`. Recorded in the report; `live` uses `release.tag` in the init diagram header. |
| `MANIFEST_JSON` | yes | The repo's `image.yml` as JSON. `name`, `build.base`, `release.tag` and the whole `test` section are read. |
| `PREVIOUS_PACKAGES_URL` | no | The previous release's `package_versions.<arch>.txt`; drives `package_diff.txt`. |
| `CI_LOG_LEVEL` | no | `DEBUG`, `INFO` (default), `WARNING`... |
| `SYFT_TIMEOUT` | no | Seconds for the syft run. Default `test.sbom_timeout` or 900. |
| `STARTUP_TIMEOUT` | no | Overrides `test.startup_timeout` (seconds to wait for `[ls.io-init] done.`). |
| `SCREENSHOT` | no | `false` skips the browser even when the manifest has `test.web`. |

From the manifest: `test.enabled` (false records the container tests as SKIP and still produces every static
asset), `test.startup_timeout`, `test.env` (extra environment for the container; `S6_VERBOSITY=2` is always
set), `test.web` (`port`, `path`, `scheme`, `auth`, `screenshot_delay`, `screenshot_timeout`) and
`test.sbom_timeout`.

Exit code is `0` whenever the harness ran, even if tests failed: the verdict is `success` in
`test.<arch>.json`. It is `1` when the harness itself could not work (no Docker daemon, image missing,
`/out` not writable) and `2` when the environment is malformed. Nothing escapes `main` as a traceback.

## Outputs

Everything goes to `/out`. Names are fixed; `<arch>` is `ARCH`.

| File | Contract | Content |
| --- | --- | --- |
| `test.<arch>.json` | `test-report.schema.json` | Tests, `success`, `startup_seconds`, `warnings`, `runtime` block, `files` (paths inside the tarball). |
| `test.<arch>.tar.gz` | | `index.html` (the report), `container.log`, `sysinfo.txt`, `package_diff.txt`, `screenshot.png` and `browser.log` when a web test ran. |
| `packages.<arch>.json.gz` | syft JSON | `syft docker:<IMAGE> -o syft-json` with `SYFT_FILE_METADATA_SELECTION=all SYFT_FILE_METADATA_DIGESTS=sha256`. |
| `files.<arch>.json.gz` | `bundle.schema.json` `$defs/file_entry` (array) | One entry per file from the syft JSON: layer index, type, size, mode, uid/gid, 16-hex digest, link target, owning package purl, mime, executable. |
| `layers.<arch>.json` | `bundle.schema.json` `$defs/layer` (array) | One per `RootFS.Layers` entry: diff_id, `created_by` from `docker history`, uncompressed size, `from_base`, `empty`, and compressed digest/size when the registry knows the image. |
| `package_versions.<arch>.txt` | text | `syft -o table`: the same shape as today's `package_versions.txt`. |
| `image-config.<arch>.json` | | `docker image inspect` as a single object. |
| `init-graph.<arch>.json` | `bundle.schema.json` `init_graph` | `{d2, units}`: the s6-rc units with type, `depends_on`, `source` (`base`, `image`, `mod`) and the `run` script for this image's own units; `d2` in the style of the old `init_diagram` readme block. |

The tests in `test.<arch>.json`, in order: Container startup, Dotnet check, uwsgi check, Get package info,
Create SBOM, File tree, Layers, Init graph, Take screenshot, Runtime probe. Statuses are `PASS`, `FAIL`,
`WARN` and `SKIP`; `success` is true when nothing failed and at least one test passed.

The `runtime` block is observed while the container sits idle after init: summed `VmRSS` of every process,
process count, listening TCP ports (`/proc/net/tcp{,6}` state `0A`), bound UDP ports (`/proc/net/udp{,6}`
state `07`), the `longrun` units under `/etc/s6-overlay/s6-rc.d`, and the `docker diff` count.

## Local usage

```bash
docker build -t ci:dev .
docker pull ghcr.io/linuxserver/baseimage-alpine:3.22
mkdir -p out
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/out":/out \
  -e IMAGE=ghcr.io/linuxserver/baseimage-alpine:3.22 \
  -e ARCH=amd64 -e MODE=dev \
  -e MANIFEST_JSON='{"schema":1,"name":"baseimage-alpine","build":{"base":"alpine","platforms":["amd64"]},"test":{"enabled":true}}' \
  ci:dev
tar xzf out/test.amd64.tar.gz -C out && xdg-open out/index.html
```

Running the Python directly (needs `syft` on `PATH`, and Chrome plus `chromedriver` for web tests):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
CI_OUT_DIR=./out IMAGE=... ARCH=amd64 MODE=dev MANIFEST_JSON='...' python -m ci
ruff check . && ruff format --check . && mypy ci tests && pytest
pytest -m integration          # the whole harness against baseimage-alpine:3.22; skips without Docker or syft
```

## Design notes

- **One run, one verdict.** No tag loops, no threads per architecture, no retries across platforms. The
  workflow calls the harness once per architecture on a native runner and merges the two reports.
- **syft runs once**, as a pinned binary against the `docker:` source over the mounted socket, producing the
  JSON (with file metadata) and the table in the same invocation. The package table, the SBOM, the file tree
  and the package diff all derive from that one run.
- **Hardlinks.** syft reports hardlinked files as regular files that share a digest, which is
  indistinguishable from identical content (every s6 `type` file says `oneshot`). The harness therefore
  streams `docker export` of a created container and reads the tar typeflags, which is exact and takes well
  under a second for a typical image. Entries that are tar hardlinks become `type: hardlink` with
  `link_target` set to the first path, so summing `size` over `type == file` gives the real footprint. If the
  export cannot be read it falls back to the `(layer, sha256)` heuristic from the ideas doc.
- **Layers.** `docker history` (via the SDK, raw byte sizes) is reconciled with `RootFS.Layers`: entries with
  a non-zero size first, then layer-producing instructions (`RUN`, `COPY`, `ADD`, `WORKDIR`). `from_base`
  compares against the FROM image when it is present locally: the labelled `io.linuxserver.image.base`
  reference first, then any local `baseimage-*` tag whose layers are the longest proper prefix of this image's.
  When no base is found the field is omitted.
- **Compressed layer sizes are best effort.** They come from `docker buildx imagetools inspect --raw`, which
  only works once the image is in a registry. For a local-only image the schema still requires `digest` and
  `size`, so each layer gets its `diff_id` as `digest` and its uncompressed size as `size`; the test message
  says which case applied. The workflow's publish step should enrich `layers.<arch>.json` after the push.
  This is a known gap.
- **Init graph.** `/etc/s6-overlay/s6-rc.d` is copied out of the image without starting it. A unit is `base`
  when the base image (found as above) also has it; without a base to compare against, the well-known base
  unit names are assumed and everything else is `image`, except `init-mod-*`/`svc-mod-*` which are `mod`.
  The d2 text reproduces the shape of the `init_diagram` blocks the Jenkins pipeline wrote: preamble,
  `dep -> unit` edges in unit order, `base -> unit` for units without dependencies, `unit -> legacy-services`
  for every longrun, then the Base Images block.
- **Dotnet and uwsgi checks** no longer warn merely because the image contains ICU or uWSGI (those warnings
  were about ARM32 under QEMU, which the playground does not build). They detect the runtime from the package
  table and look for its known failure lines in the container log; a hit is a `WARN` and lands in `warnings`.
- **Image.** `ghcr.io/linuxserver/baseimage-ubuntu:noble` (and `arm64v8-noble`), in the usual single `RUN`
  chain. Noble ships Python 3.12, so `python3.13` and `python3.13-venv` come from the deadsnakes PPA and the
  harness lives in the `/lsiopy` venv the base already puts on `PATH`. syft is a pinned binary from its GitHub
  release (`SYFT_VERSION` build arg); the docker CLI and buildx plugin come from Docker's apt repo so
  `docker buildx imagetools inspect` works. `ENTRYPOINT` is `python3 -m ci`; the s6 init of the base is not used.
- **Browser.** Headless Chrome through Selenium, as before. amd64 installs Google Chrome plus the matching
  ChromeDriver from Chrome for Testing. Ubuntu noble ships only snap stubs for Chromium, so `Dockerfile.aarch64`
  takes `chromium` and `chromium-driver` from the xtradeb PPA (arm64 builds for noble). The report keeps
  `screenshot.png` as PNG and the console log as JSON in `browser.log`.
- **Report.** `test.<arch>.json` is validated against `test-report.schema.json` when the `contracts` package
  is installed; violations are logged, the file is still written. `image` in the report is the manifest
  `name`; `tag` is the tag part of `IMAGE`.
