# AGENTS.md - ci

## Purpose

This repo is the container test harness for the `linuxserver-playground` organisation. The shared GitHub
Actions workflow (`workflows` repo) runs the published image once per architecture against a freshly built
image and bundles what it writes into the GitHub Release. The hub reads `test.<arch>.json`,
`files.<arch>.json.gz`, `layers.<arch>.json`, `packages.<arch>.json.gz` and `init-graph.<arch>.json`; the
publish job attaches `sbom.<arch>.spdx.json` to the image;
humans open `index.html` from `test.<arch>.tar.gz`. Changing an output name or shape breaks both consumers.

## Layout

| Path | What |
| --- | --- |
| `ci/__main__.py` | Entry point: environment to `Settings`, exit codes, nothing escapes as a traceback. |
| `ci/harness.py` | The run: container tests, syft outputs, static assets, report. One `TestResult` per step. |
| `ci/config.py` | `Settings` from env plus the pydantic model of the manifest parts we read. |
| `ci/docker_client.py` | Thin wrapper over the docker SDK: inspect, history, run, exec, copy. |
| `ci/startup.py`, `ci/checks.py`, `ci/probe.py`, `ci/browser.py` | Container-side tests: init wait, dotnet/uwsgi/sysinfo, runtime probe, Selenium screenshot. |
| `ci/packages.py`, `ci/dumper.py`, `ci/layers.py`, `ci/initgraph.py` | Bundle asset emitters (`dumper.py` runs the dumper binary for the file tree). |
| `ci/report.py`, `ci/template.html` | `test.<arch>.json`, HTML report, tarball. |
| `tests/` | pytest. Unit tests mock the SDK; `test_integration.py` runs the whole harness (marker `integration`). |
| `Dockerfile`, `Dockerfile.aarch64` | The published image: Ubuntu noble base, Chrome/Chromium, docker CLI, pinned syft. |

## What you may edit

Everything, by pull request. The interface in `README.md` (environment variables, output file names, test
names and order) is shared with the `workflows` and `hub` repos: change it together with them, not alone.
`LICENSE` stays GPL-3.0. Do not add S3, Jenkins, Docker Hub or any namespace outside
`linuxserver-playground` (base images come from `ghcr.io/linuxserver/baseimage-*`).

## How to validate

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy ci tests && pytest
docker build -t ci:dev . && docker pull ghcr.io/linuxserver/baseimage-alpine:3.22
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD/out":/out \
  -e IMAGE=ghcr.io/linuxserver/baseimage-alpine:3.22 -e ARCH=amd64 -e MODE=dev \
  -e MANIFEST_JSON='{"schema":1,"name":"baseimage-alpine","build":{"base":"alpine","platforms":["amd64"]},"test":{"enabled":true}}' ci:dev
```

The integration test needs a Docker daemon, `syft` on `PATH` and the
[dumper](https://github.com/linuxserver-playground/dumper) binary (`DUMPER` or on `PATH`), and skips otherwise. `Dockerfile.aarch64`
can only be built on an arm64 host (or with QEMU binfmt installed).

## Contracts

Consumes `image.schema.json` (the `test`, `build`, `release` and `name` parts of the manifest arrive as
`MANIFEST_JSON`). Produces `test-report.schema.json` and the `bundle.schema.json` definitions `$defs/layer`,
`$defs/file_tree` and `init_graph`. The schemas live in
[linuxserver-playground/builder](https://github.com/linuxserver-playground/builder); read its
`CONVENTIONS.md` §6 before changing an output.

## Pull request rules

Disclose AI use in the description. One change per PR. Commit subjects are short imperative sentences
without prefixes, emoji or trailers. Do not commit generated output, credentials or `out/`.
