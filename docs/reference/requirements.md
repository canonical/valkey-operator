(requirements)=

# Requirements

## Juju version

The charm currently runs on and is tested against
[Juju 3.6 LTS](https://github.com/juju/juju/releases).

The minimum supported Juju version is [Juju 3.6+](https://github.com/juju/juju/releases).

## Hardware

The charm provides dual-substrate compatibility and can be deployed on Kubernetes and machine substrates.

Actual hardware requirements will depend on the workload you are running against your database.

The charm is based on the [valkey-charmed snap](https://snapcraft.io/valkey-charmed) and the
[valkey-charmed rock](https://github.com/canonical/valkey-artifacts/pkgs/container/valkey-charmed).

It currently supports the following architectures:

* `amd64`
* `arm64`

## Charm Interfaces

* Relation interface `valkey_client`
  * support for `data-interfaces` v1
  * backward compatibility for `data-interfaces` v0
* Integration with a TLS provider charm
  * `tls-certificates` interface v4
  * `certificate_transfer` interface v1
