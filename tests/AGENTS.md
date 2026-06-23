# Integration & spread test guidance

Extends the root `AGENTS.md`. (`CLAUDE.md` and `GEMINI.md` in this directory are symlinks to this
file so that Claude Code and Gemini CLI pick it up too.)

- `tox run -e integration -- <test-path> --substrate {k8s,vm}` — a test path IS honored here
  (unlike the unit env); `--substrate` defaults to `k8s` (defined in `tests/conftest.py`).
- Prerequisites: a bootstrapped Juju controller (microk8s for k8s, lxd for vm), a built charm at
  the repo root, and the built requirer-charm
  (`charmcraft pack` inside `tests/integration/clients/requirer-charm/` →
  `requirer-charm_ubuntu@24.04-<arch>.charm`). The requirer-charm ("glide-runner") drives
  continuous writes with `valkey-glide` to validate HA scenarios.
- The integration env's `commands_pre` runs `sudo apt install wget`, downloads a Valkey tarball
  from download.valkey.io, and installs `valkey-cli` into `/usr/local/bin` — all before any
  controller check. Building the integration dependency group needs
  `libprotobuf-dev protobuf-compiler` and a Rust toolchain (CI pins 1.90.0).
- When `$CI` is unset, tox runs `juju add-model testing` — a second local run fails if the model
  still exists; `juju destroy-model testing` between runs, or invoke pytest directly from
  `.tox/integration/bin/pytest`.
- Waits are long by design: `juju.wait_timeout = 1000` seconds in `tests/integration/conftest.py`
  — a run sitting quiet for 15+ minutes is usually a legitimate wait, not a hang.
- Spread: each `tests/spread/{k8s,vm}/<test>.py/task.yaml` just calls the tox command above on a
  concierge-prepared backend. The `github-ci` backend is `manual: true` (CI-only, one runner per
  job, ~75-min/job timeout); the local backend is `lxd-vm`. CI pins Juju via
  `CONCIERGE_JUJU_CHANNEL: 3.6/stable` and microk8s `1.34-strict/stable`.
