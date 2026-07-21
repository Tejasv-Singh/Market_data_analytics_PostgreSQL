"""Connection + server management.

By default runs a portable PostgreSQL (pgserver) whose data lives in ./pgdata.
Set DATABASE_URL to point everything at any other Postgres instead.

    python db.py start      # start (and tune on first run)
    python db.py stop
    python db.py uri        # print the connection string
    python db.py psql       # interactive psql shell
"""
import os
import subprocess
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent
PGDATA = ROOT / "pgdata"
DBNAME = "market"

# Settings sized for a 16 GB / 16-core laptop. Applied once via ALTER SYSTEM.
TUNING = {
    "shared_buffers": "2GB",
    "effective_cache_size": "8GB",
    "work_mem": "128MB",
    "maintenance_work_mem": "1GB",
    "max_wal_size": "8GB",
    "checkpoint_timeout": "30min",
    "random_page_cost": "1.1",          # SSD
    "max_parallel_workers_per_gather": "4",
    "max_parallel_maintenance_workers": "4",
    "jit": "off",                       # JIT compile time adds noise to small queries
    "track_io_timing": "on",            # I/O timings in EXPLAIN (ANALYZE, BUFFERS)
}


def _server():
    import pgserver
    return pgserver.get_server(PGDATA, cleanup_mode=None)


def _pg_ctl(*args):
    import pgserver
    pgserver.pg_ctl(list(args), pgdata=PGDATA)


def admin_uri():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return _server().get_uri()


def uri():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return _server().get_uri(DBNAME)


def start():
    if os.environ.get("DATABASE_URL"):
        print("DATABASE_URL set; nothing to start.")
        return
    srv = _server()
    with psycopg.connect(srv.get_uri(), autocommit=True) as conn:
        current = conn.execute("SHOW shared_buffers").fetchone()[0]
        if current != TUNING["shared_buffers"]:
            print("Applying tuning settings and restarting...")
            for k, v in TUNING.items():
                conn.execute(f"ALTER SYSTEM SET {k} = '{v}'")
            needs_restart = True
        else:
            needs_restart = False
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DBNAME,)).fetchone()
        if not exists:
            conn.execute(f"CREATE DATABASE {DBNAME}")
    if needs_restart:
        stop()
        import pgserver
        pgserver.postgres_server.PostgresServer._instances.clear()
        srv = _server()
    print(srv.get_uri(DBNAME))


def stop():
    if (PGDATA / "postmaster.pid").exists():
        _pg_ctl("-w", "stop", "-m", "fast")


def connect(**kw):
    return psycopg.connect(uri(), **kw)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "uri"
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "uri":
        print(uri())
    elif cmd == "psql":
        import pgserver
        psql = Path(pgserver._commands.POSTGRES_BIN_PATH) / "psql"
        subprocess.call([str(psql), uri()])
    else:
        sys.exit(__doc__)
