"""Runtime hook: cap OpenMP / MKL pools at 1 thread per worker.

The daemon spawns N workers; each worker independently invokes torch /
numpy. If we let OpenMP grow its own thread pool inside each worker,
total threads = N × physical cores → kernel scheduling thrash. Cap at
1 here so daemon-level parallelism comes purely from the N worker
threads. Mirrors `OMP_NUM_THREADS=1` in the legacy run_daemon.bat.

Only applied to the daemon exe (api / db_init / db_backup don't need it).
"""
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
