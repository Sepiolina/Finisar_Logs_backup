# Performance

## Results

Measured with `bench.py`: about 2 KiB per file, five subfolders, a full run
from scan to cleanup, then a check that nothing is left in the source or
staging. 4-core Linux VM, local SSD, MOCHI 0.9.13. The VM's disk is noisy, so
expect ±10%.

| Run | This tool | Finisar_move_log_files (Rust, its README) |
|---|---|---|
| 20,000 files, 1 month | **9.6 s** (2,100 files/s) | 10.1 s |
| then append 2,000 | 4.5 s | 2.6 s |
| 100,000 files, 1 month | **72 s** | 71 s |
| then append 4,000 | 20 s | 14 s |
| 300,000 files, 3 months | **140 s** (2,100 files/s) | 158 s (older build) |
| then append 6,000 | 22 s | not measured |

Peak memory for 300,000 files: about 0.7 GB for the Python process. Each
`mochi` process committing a 100,000-file month peaked at about 1.1 GB, and up
to `WORKERS` months are committed at once.

**So yes, it handles a lot of files.** A first run over 300,000 files takes
about 2–3 minutes. Daily or weekly runs with a few thousand new files take
about 20 seconds, and that time is mostly checking the existing month (see
below).

## Where the time goes

Breakdown of the 300,000-file run:

| Step | Time | What it is |
|---|---|---|
| Scan | 12 s | Listing folders and matching names. Linear in the number of files in the source folders. |
| Stage | 21 s | Renames, plus an `fsync` of the manifest every 2,000 files |
| Commit | 107 s | Mostly MOCHI's own work: writing the archive, then verifying it. Months run side by side. |

Inside the commit of a 100,000-file month:

| Part | Time |
|---|---|
| `mochi create` | ~33 s |
| BLAKE3 of staged files | ~9 s, **runs at the same time as the commit** |
| `mochi list` of the head | ~2.5 s |
| `mochi verify --level content` | ~9.5 s |
| SQLite index | <1 s |
| Deleting staged copies | a few seconds |

### Why appends cost more than in the Rust original

After an append, the original verifies only the new commit, using a MOCHI
library call. The `mochi` command line has no such option, so this tool verifies
the **whole** month at content level each run. For a 100,000-file month that is
about 9.5 s whatever the number of new files. Opening the month's file list
costs about 6 s on its own, even at the lightest level, so a lighter level would
save little.

## What was tried

| Change | Result | Kept? |
|---|---|---|
| Hash staged files while `mochi` commits, not before | First run of 100,000 files: 83 s → 72 s | yes |
| Check the head before committing only when resuming | Saves one `mochi list` (~2.5 s) per month per run | yes |
| Stream the scan instead of listing every folder first | Lower memory, same speed | yes |
| Hash in chunks of 500 files per task | 300,000 files: peak memory 1.15 GB → 0.69 GB | yes |
| Renames and deletes on 4–16 threads | **Slower** here: 50,000 renames took 0.8 s on 1 thread, 2–3 s on 4–8 threads | local disk: no; network drive: yes (below) |
| Copy staging to a local disk on a network drive | Simulated network (below): 20 s → 6.5 s for 5,000 files | network drive: yes |
| List folders on 16 threads, compare paths without `resolve()` | Simulated network: scan 0.8 s → 0.3 s for 40 folders | network drive: yes |

### On a network drive

A run on a shared network drive staged about 11 files/s (12,000 files in 18
minutes), and its scan listed about 230 files/s, against 25,000 files/s on the
local SSD above. Each rename is a few network round trips, and one thread
spends nearly all of its time waiting for them. So when `source_dir` is a
network drive (a mapped drive letter or `//server/share`), renames, the
"is it staged?" checks and the deletes after the commit run on 16 threads
(`file_threads` in config.json changes this). On Windows the rename itself refuses
to overwrite, so the separate "does the target exist?" check, one more round
trip per file, is skipped there.

Then copy staging (the default on a network drive) cut the remaining trips.
Moving files on the share left `mochi` and the hash check reading every staged
file over the network, and the rename itself was a round trip. Now each file is
listed, read once (copied to `output_dir/.staging` on the local disk), checked
and deleted: everything else is local. Folders are listed 16 at a time too.

Simulated with a 3 ms delay on every call in the source folder (listing,
rename, copy, stat, delete, and each read by the hash check and a stand-in
`mochi`), 5,000 files in 40 folders:

| Setup | Total | Stage | Commit and cleanup |
|---|---|---|---|
| Move, 1 thread (before) | 79.8 s | 46.2 s | 32.7 s |
| Move, 16 threads | 20.4 s | 3.0 s | 17.0 s |
| Copy, 1 thread | 82.2 s | 47.4 s | 33.6 s |
| **Copy, 16 threads (default on a network drive)** | **6.5 s** | 3.8 s | 2.3 s |

A real share's delay, and how many calls at once it handles well, differ:
measure there, and try `"file_threads"` 8 or 32.

The fastest setup is still to run the backup **on the file server itself**,
against its local disk: then nothing crosses the network. Antivirus that scans
each file as it is renamed also slows staging down; excluding `source_dir`,
staging and `output_dir` from real-time scanning helps.

### Next batch staged while this one commits

A run works in batches. Before, each batch was staged, then committed, verified and cleaned up,
and only then was the next batch staged. Now the next batch is staged (copied from the NAS, or
renamed) on a second thread **while** the current one commits, into a second staging folder next
to the first (`.staging-2`). On a NAS both steps mostly wait on the network or on `mochi`, so they
overlap well. Simulated with about 2 s of staging and 2 s of commit per batch, 4 batches of 1,000
files: **19.0 s → 12.6 s**. The saving per batch is about the shorter of the two steps.

- Copy staging: the next batch is copied only if the staging drive has room for it beside the
  batch already there; otherwise it waits for the commit, as before (logged).
- Stop: a commit that has begun is finished; the batch staging meanwhile stops at its next file
  and is finished from staging by the next run, like any staged batch.
- A failure in the commit: the batch staging meanwhile is completed first, then the run stops.
  Both batches wait in their staging folders and the next run finishes them.

## Measure on your own machine

```bash
MOCHI_BIN=/path/to/mochi python bench.py 100000 1 D:/bench_tmp --append 4000
```

This creates the files under the given folder (it **deletes that folder first**),
runs a backup, and prints the time per step. Use a folder on the same kind of
disk as your real logs.

## Tuning

- **Run more often** to keep each run small. Appends cost about the same whatever their size.
- **One archive per month** keeps each archive's file list, and so verify time, bounded.
- `WORKERS` in `logs_backup.py` (CPU count, at most 8) sets hashing threads and
  how many months commit at once. Lower it on a busy PC.
- `MANIFEST_BATCH` (2,000) sets how often the manifest is synced while staging.
  Larger means fewer disk syncs and the same safety.
