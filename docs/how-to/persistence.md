(how-to-persistence)=
# How to configure persistence

Valkey keeps its dataset in memory. The `persistence-mode` config option decides what Valkey also writes to disk.

| Mode            | What Valkey writes to disk                                                  | Use it when                                                                         |
| --------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `RDB` (default) | A point-in-time snapshot (`dump.rdb`) at the intervals in `rdb-save-points` | Losing the writes since the last snapshot is acceptable                             |
| `AOF`           | An append-only file with every write, flushed per `aof-appendfsync`         | Losing about one second of writes is acceptable, with the default `aof-appendfsync` |
| `RDB+AOF`       | Both. Valkey loads the append-only file when it starts                      | You want AOF durability and snapshots                                               |
| `None`          | Nothing during normal operation                                             | The data is a cache you can rebuild                                                 |

```{warning}
When `persistence-mode` is set to `None` or `RDB`, a primary crash can cause data loss on all units, including replicas. Use `None` only for data you can rebuild.

Valkey restarts after approximately `1` second on Kubernetes and `20` seconds on machines: before Sentinel’s 30-second failover window expires. If it restarts before failover, it resumes as the primary with an empty dataset or an older snapshot. Replicas then synchronize with the primary, replacing their data with that dataset.
```

## Change the mode

```shell
juju config valkey persistence-mode=AOF
```

The charm applies the change on every unit without a restart. When AOF is switched on, Valkey first writes the whole dataset to a new append-only file. When AOF is switched off and snapshots stay on, Valkey first saves a new `dump.rdb`. Until that file is complete, `valkey.conf` keeps the previous settings, so a restart in between still loads current data. Once the switch is done, the charm deletes the files of the mode it left, for example `dump.rdb` after a switch to `AOF`.

## Tune snapshots and disk syncs

`rdb-save-points` takes pairs of `<seconds> <changes>`. Valkey saves once that many seconds have passed and at least that many writes happened since the last save:

```shell
juju config valkey rdb-save-points="3600 1 300 100"
```

`aof-appendfsync` sets how often the append-only file reaches the disk: `always` after every write, `everysec` once per second (default), or `no` to leave it to the operating system:

```shell
juju config valkey aof-appendfsync=always
```

If a value is invalid, the application shows a blocked status and the charm keeps the last valid settings. The charm checks `rdb-save-points` only in the `RDB` and `RDB+AOF` modes.

## Restore a backup

[Backups](how-to-backup) are RDB snapshots, and a restore works in every mode. In an AOF mode, the restore deletes the current append-only file, the unit loads the backup, and the charm then switches AOF back on.

## Corrupted append-only files

A corrupted append-only file heals itself on the next `update-status` when another healthy unit exists. If the affected unit is the primary, the charm fails over first. It then moves the corrupted files to the `archive` storage, replacing the previous copy, and rebuilds the unit with a full sync from the primary. Writes the failed primary never replicated are not in the cluster, but the archived copy keeps them. If no healthy unit exists, the unit shows a blocked status. If the archive storage has no space for the copy, the unit shows a blocked status and keeps the corrupted files in place until you free space on that storage or allocate more. If healing fails for another reason, the unit shows a maintenance status and retries.

The copy, `appendonlydir.corrupt`, stays on the `archive` storage until you delete it. During a restore, the primary keeps its current `dump.rdb` on that storage as a rollback copy. Delete `appendonlydir.corrupt` once you no longer need it, or a later restore can run out of space.
