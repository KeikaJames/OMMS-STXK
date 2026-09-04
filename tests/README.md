# Isolated concurrency tests

`stress_registration.py` validates the Rust `/api/register_club` hot path with
one independent session and one request per virtual student. It reports the
business outcomes, HTTP/transport errors, p50/p95/p99 latency, SQLite counters,
Redis stock, confirmed-registration mirrors (including the exact
`<club_id>|<operation_id>` value), remaining reservations, and `seat:op` leases. Any correctness
or availability invariant failure exits with status 1; environment setup
failures exit with status 2.

The test does **not** use the repository database or Redis port 6379. Every run:

1. creates a temporary SQLite database with one club, the requested users, the
   `operation_id` column/index, and the final capacity trigger;
2. starts a private `redis-server` on an OS-selected loopback port other than
   6379, with persistence disabled and its directory under the same temporary
   folder;
3. writes a distinct `sess:{token}` value for every virtual student;
4. starts the selected Rust release on another ephemeral loopback port;
5. removes the processes, database, Redis files, and logs on exit.

Prerequisites:

```bash
cargo build --release --manifest-path club-hot/Cargo.toml
redis-server --version
```

Small smoke test:

```bash
python3 tests/stress_registration.py --seats 3 --users 12 --concurrency 6
```

Larger synchronized contest:

```bash
python3 tests/stress_registration.py \
  --seats 30 \
  --users 1000 \
  --concurrency 300
```

To validate a particular release artifact:

```bash
python3 tests/stress_registration.py \
  --rust-bin /absolute/path/to/club-hot \
  --seats 30 --users 300 --concurrency 100
```

Use `--help` for client/server timeouts and the Rust
`MAX_CONCURRENCY`/reservation-TTL controls. The test default for `RESV_TTL` is
60 seconds and its default `MAX_CONCURRENCY` is 1024, matching the service. This tool deliberately tests the
Rust service directly. It does not measure Nginx's per-session limiter, TLS, or
the Python fallback; those need separate edge scenarios so an edge policy is not
misreported as Rust capacity.

The Python race regression suite launches the same kind of isolated Redis and
temporary database, then exercises the fallback HTTP service:

```bash
PYTHONDONTWRITEBYTECODE=1 \
python3 -m unittest discover -s tests -p 'test_python_races.py' -v
```

It covers 100 concurrent cancels of one registration, an intentionally inflated
Redis stock value versus the SQLite capacity trigger, and exact
`club_id|operation_id` confirmation mirroring. It also replays a completed
same-club request and requires an idempotent success without a second DB row or
stock decrement. A fourth case proves that understock makes `/readyz` fail and
wakes the maintenance-fenced background reconciler.
