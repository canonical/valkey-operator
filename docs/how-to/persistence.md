(how-to-persistence)=
# How to configure persistence

Valkey keeps its dataset in memory. The `persistence-mode` config option decides what Valkey also writes to disk.

| Mode            | What Valkey writes                                                          | Use it when                                             |
| --------------- | --------------------------------------------------------------------------- | ------------------------------------------------------- |
| `RDB` (default) | A point-in-time snapshot (`dump.rdb`) at the intervals in `rdb-save-points` | Losing the writes since the last snapshot is acceptable |
| `AOF`           | An append-only file with every write, flushed per `aof-appendfsync`         | You need at most about one second of data loss          |
| `RDB+AOF`       | Both. The append-only file wins when Valkey starts                          | You want AOF durability plus snapshots                  |
| `None`          | Nothing during normal operation                                             | The data is a cache you can rebuild                     |

```{warning}
In `None` mode, a crash of the primary can wipe the data on every unit, not only on the one that
crashed. Valkey restarts after about 1 second on Kubernetes and 20 seconds on machines, before
Sentinel's 30-second window to fail it over runs out. It comes back as the primary, empty or with an
old snapshot, and the replicas copy that dataset. Only use `None` for data you can rebuild.
```

## Change the mode

```shell
juju config valkey persistence-mode=AOF
```

The charm applies the change on every unit without a restart. When AOF is switched on, Valkey first writes the whole dataset to a new append-only file. Until that file is complete, the unit keeps its previous settings on disk.

## Tune snapshots and disk syncs

`rdb-save-points` takes pairs of `<seconds> <changes>`. Valkey saves when the given number of seconds elapsed and at least the given number of writes happened:

```shell
juju config valkey rdb-save-points="3600 1 300 100"
```

`aof-appendfsync` sets how often the append-only file reaches the disk: `always` after every write, `everysec` once per second (default), or `no` to leave it to the operating system:

```shell
juju config valkey aof-appendfsync=always
```

If a value is invalid, the application shows a blocked status and the charm keeps the last valid settings.

## Behavior to know

- **Removing a unit always saves a snapshot**, in every mode, so the data stays on the storage volume. With AOF on, the charm also waits for any running append-only file rewrite to finish before it stops Valkey.
- **Restarts the charm makes keep the data.** In `None` mode the charm saves a snapshot before it restarts Valkey, for example to turn TLS on, so the unit doesn't come back empty.
- **Crashes follow the mode.** In `AOF` mode only the append-only file is on disk, in `RDB+AOF` both files are, and in `None` nothing new is saved.
- **Reusing a storage volume** loads the newest data on it. In `AOF` and `RDB+AOF` mode, the unit loads the append-only file when it's newer than `dump.rdb`, for example after a unit was lost without a clean removal. Otherwise, and in `RDB` and `None` mode, it loads `dump.rdb`, which a clean removal always saves last.
- **Backups and restores work in every mode.** A backup is always a snapshot. During a restore, the charm starts the primary with AOF off so it loads the snapshot, then switches AOF back on.
- **A corrupted append-only file heals itself** when another healthy unit exists. If the affected unit is the primary, the charm fails over first. It then copies the corrupted files to the archive storage (`appendonlydir.corrupt`, replacing any previous copy), deletes them from the data storage, and rebuilds the unit with a full sync from the primary. If no healthy unit exists, the unit shows a blocked status. If healing fails, the unit shows a maintenance status and retries.
