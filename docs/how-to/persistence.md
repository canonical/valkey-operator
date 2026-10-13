(how-to-persistence)=
# How to configure persistence

Valkey keeps its dataset in memory. The `persistence-mode` config option decides what Valkey also writes to disk.

| Mode | What Valkey writes | Use it when |
|---|---|---|
| `RDB` (default) | A point-in-time snapshot (`dump.rdb`) at the intervals in `rdb-save-points` | Losing the writes since the last snapshot is acceptable |
| `AOF` | An append-only file with every write, flushed per `aof-appendfsync` | You need at most about one second of data loss |
| `RDB+AOF` | Both. The append-only file wins when Valkey starts | You want AOF durability plus snapshots |
| `None` | Nothing during normal operation | The data is a cache you can rebuild |

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
- **Crashes follow the mode.** In `AOF` mode only the append-only file is on disk, in `RDB+AOF` both files are, and in `None` nothing new is saved.
- **Reusing a storage volume** loads `dump.rdb` in every mode, then the unit switches to the configured mode. Removing a unit saves a snapshot first, so `dump.rdb` holds the latest data. After a unit is lost without a clean removal, writes newer than the last snapshot are lost, even in `AOF` mode.
- **Backups and restores work in every mode.** A backup is always a snapshot. During a restore, the charm starts the primary with AOF off so it loads the snapshot, then switches AOF back on.
- **A corrupted append-only file heals itself** when another healthy unit exists. If the affected unit is the primary, the charm fails over first. It then moves the corrupted file to the archive storage (`appendonlydir.corrupt`, replacing any previous copy) and rebuilds the unit with a full sync from the primary. If no healthy unit exists, the unit shows a blocked status.
