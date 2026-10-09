(how-to-refresh)=

# How to refresh (upgrade)

```{admonition} Emergency stop button
:class: attention
Use `juju config <app name> pause-after-unit-refresh=all` to halt an in-progress refresh.
Then, consider [rolling back](how-to-refresh-roll-back)
```

Charmed Valkey supports minor version in-place refresh via the [`juju refresh`](https://documentation.ubuntu.com/juju/3.6/reference/juju-cli/list-of-juju-cli-commands/refresh/#details) command.

## Determine which version to refresh to

Get the current charm revision of the application with [`juju status`](https://documentation.ubuntu.com/juju/3.6/reference/juju-cli/list-of-juju-cli-commands/status/).

### Supported refreshes

These refreshes should be supported:
* Minor in-place upgrade
* Any refresh from one `stable` version to a later `stable` version in the same channel (e.g. `9/stable`)


### Unsupported refreshes

These are examples of refreshes that are not supported in-place.
In some of these cases, it may be possible to perform an out-of-place upgrade or downgrade.

* Minor in-place downgrade
* Major in-place upgrade
* Major in-place downgrade
* Any refresh from or to a non-stable version (e.g. 9/edge)

(how-to-refresh-backup)=

## Create a backup

See [](back-up-and-restore.md).

## Read the rollback instructions

In the event that something goes wrong (e.g. the refresh fails, the new version of Valkey has any issues, 
a database client is incompatible with the new version), you may want to quickly roll back.

Prepare for this possibility by reading through the entire refresh documentation — with special attention to the 
[](how-to-refresh-halt) and [](how-to-refresh-roll-back) sections — before starting the refresh.

## Review release notes

For every charm version between the version that you are refreshing from and to, review the release notes 
to understand what changed and if any action is required from you before, during, or after the refresh.

If the Valkey versions that you are refreshing from and to are different, refer to the 
[upstream Valkey release notes](https://github.com/valkey-io/valkey/releases) to understand what changed and if 
any action is required from you.

(how-to-refresh-staging)=

## Test in a staging environment

We recommend testing the entire refresh procedure in a staging environment before refreshing your production environment.

In a staging environment, we also encourage you to simulate failure of the refresh and to practice recovery by restoring 
from [the backup](how-to-refresh-backup).

## Check that clients are compatible

Ensure that your clients are compatible with the Valkey version that you're refreshing to.
It may be necessary to refresh your clients before refreshing Valkey.

## Inform users and schedule a maintenance window

Tell your users when you will perform the refresh and remain in contact with them so that you are aware of any issues.

If possible, schedule a maintenance window during a period of low traffic.
The duration of the refresh may depend on the size of your data and volume of traffic.
To estimate the duration, we recommend [testing on a staging environment](how-to-refresh-staging).

## Consider scaling up

During the refresh of the application, units will be restarted one by one.
While a unit is restarting, the performance of the cluster may be degraded.

To ensure that the cluster can handle all traffic during the refresh, consider scaling up the application by one unit.

```{note}
Charmed Valkey does also support scaling up while a refresh is in progress.
```

(how-to-refresh-pre-check)=

## Pre-refresh check

Run the `pre-refresh-check` action on the leader unit to prepare the application for refresh.

```shell
juju run valkey/leader pre-refresh-check
```

If the action does not succeed, do not refresh.

If the action succeeds, copy down the rollback command.
Keep the command available in case you need to [roll back](how-to-refresh-roll-back).

(how-to-refresh-manual-checks)=

## Configure `pause-after-unit-refresh`

After each unit is refreshed, the charm will perform automatic health checks.
We recommend supplementing the automatic checks with manual checks.

Examples of manual checks:
* Database clients are healthy and can connect to the refreshed units
* Writes per second and resource consumption (CPU, memory, disk) are similar on refreshed and non-refreshed units
* Consider leaving the application in a partially-refreshed state (only some units refreshed) for an extended period 
 and monitoring that the new version is stable in your environment

To facilitate your manual checks, the application can be configured to pause the refresh and wait for your confirmation.

Set the `pause-after-unit-refresh` config option to:
* `all` to wait for your confirmation after each unit refreshes
* `first` (default) to wait for your confirmation once, after the first unit refreshes
* `none` to never wait for your confirmation

For example:
```shell
juju config valkey pause-after-unit-refresh=all
```

```{note}
If the charm's automatic health checks fail, the refresh will be paused (until those health checks succeed),
regardless of the value of the `pause-after-unit-refresh` config option.
```

## Avoid operations while a refresh is in progress

While a refresh is in progress, the application is in a vulnerable state.

```{caution}
It is not supported to restore a backup while a refresh is in progress.
```

These operations are supported but not recommended while a refresh is in progress:
* Enabling or disabling TLS
* Adding or removing client relations
* Adding or removing units
* Changes to config values (except `pause-after-unit-refresh`)
* Updating the admin password or private keys for TLS certificates

## Start the refresh

Use `juju refresh` and specify the charm revision that you are refreshing to.

```shell
juju refresh valkey --revision <your-target-revision>
```

(how-to-refresh-halt)=

## Halt the refresh

If something goes wrong, halt the refresh by running:

```shell
juju config valkey pause-after-unit-refresh=all
```

In the command above, replace `valkey` with the name of the Juju application.

Next, assess the situation and plan the recovery.
Often, the safest recovery path is to [roll back](how-to-refresh-roll-back).
Consider [contacting us](https://matrix.to/#/#charmhub-data-platform:ubuntu.com).

(how-to-refresh-roll-back)=

## Roll back

If something went wrong, the safest recovery path is often to roll back to the original version.

First, [halt the refresh](how-to-refresh-halt).

Run the rollback command [you copied down earlier](how-to-refresh-pre-check).
In most cases, the rollback command is also displayed in the application's status message in `juju status`.

### Resume the rollback

If more than one unit was refreshed before the rollback was started and `pause-after-unit-refresh` 
is set to `all` or `first`, your manual confirmation will be needed to complete the rollback.
The procedure for the rollback is the same as described in [](how-to-refresh-monitor).

### Reflect

After the application has been rolled back, and you have confirmed that service has been fully restored, 
investigate what went wrong.

If applicable, please file a [bug report](https://github.com/canonical/valkey-operator/issues).

Once you understand what went wrong and have tested that it has been fixed, the refresh can be attempted again.

(how-to-refresh-monitor)=

## Monitor the refresh

Use `juju status` to monitor the progress of the refresh.

In some cases, it may take a few minutes for the statuses to update after the refresh has started.

If the application status or any of the unit statuses are `blocked`, your action is required.
Follow the instructions in the status messages.

If the application status or any of the unit statuses are `error`, your action may be required.
Monitor `juju debug-log`.
The error may have been a temporary issue.
If the error persists, your action is required — consider [rolling back](how-to-refresh-roll-back).

Monitor the refresh until it successfully finishes.
When the refresh completes, the application status will go from a message beginning with "Refreshing" 
to an `active` status with no message.

### Resume refresh

If `pause-after-unit-refresh` is set to `all` or `first` (default), your confirmation will be needed during the refresh.

The application status in `juju status` will instruct you when your confirmation is needed with the `resume-refresh` action.

Before running the `resume-refresh` action:
* Wait until all the application's unit agent statuses are `idle`
* Wait until all the refreshed units' workload statuses are `active`
* Perform [manual checks](how-to-refresh-manual-checks) to ensure that everything is healthy

Example of running the `resume-refresh` action on unit 1:

```shell
juju run valkey/1 resume-refresh
```