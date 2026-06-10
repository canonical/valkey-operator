---
description: Build the charm and run integration/HA tests against a local Juju controller. Use
  when asked to run, debug, or reproduce integration test failures.
disable-model-invocation: true
argument-hint: [test-path] [k8s|vm]
---

Run integration tests for $0 (default `tests/integration/test_charm.py`) on substrate $1
(default `k8s`):

1. Verify a bootstrapped controller exists: `juju controllers` (microk8s for k8s, lxd for vm).
   If none, STOP and ask the user — do not bootstrap one unsolicited.
2. Verify a built charm exists at the repo root (`ls *.charm`); if not, run `charmcraftcache pack`
   (or `charmcraft pack`) — warn the user this is a long Rust build.
3. Verify the requirer charm is built (`ls tests/integration/clients/requirer-charm/*.charm`);
   if not, `charmcraft pack` inside that directory.
4. If a `testing` model is left over from a previous local run, `juju destroy-model testing`
   (tox re-creates it when `$CI` is unset).
5. `tox run -e integration -- $0 --substrate $1`
6. On completion or failure, capture `juju status --format yaml` and
   `juju debug-log --replay --no-tail | tail -200`.
7. Clean up only what you created: remove the `testing` model if you made it; NEVER destroy the
   user's controller.
