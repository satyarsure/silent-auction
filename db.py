"""Lakebase (PostgreSQL) data-access layer for the anonymous auction app.

Connection strategy for Databricks Apps + Lakebase:
- Host / database / user come from the auto-injected PG* env vars when the
  Lakebase resource is attached to the app.
- The *password* is a short-lived OAuth token (≈1h). A long-running app must
  mint fresh tokens, so we generate one via the SDK and cache it for 45 min
  rather than relying on the startup-injected PGPASSWORD.
- Locally / as a fallback we resolve the host from the instance name.

Scale-to-zero: the Lakebase instance pauses when idle. The first connection
after an idle period can be refused / time out while it resumes, so get_conn
retries with backoff (and proactively asks the instance to start), and callers
can surface a friendly "waking up" message via ColdStartError.

Schema is created idempotently on first use (init_schema).
"""
from __future__ import annotations

import os
import time
import uuid
import threading
from contextlib import contextmanager
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row
from databricks.sdk import WorkspaceClient

INSTANCE_NAME = os.getenv("LAKEBASE_INSTANCE_NAME", "auction-lakebase")
DB_NAME = os.getenv("PGDATABASE", "databricks_postgres")
DB_PORT = os.getenv("PGPORT", "5432")

# Scale-to-zero cold-start tuning.
CONNECT_MAX_WAIT = float(os.getenv("LAKEBASE_CONNECT_MAX_WAIT", "90"))  # seconds
CONNECT_TIMEOUT = int(os.getenv("LAKEBASE_CONNECT_TIMEOUT", "10"))      # per attempt

_w: WorkspaceClient | None = None
_token = {"value": None, "exp": 0.0}
_host_cache: dict[str, str] = {}
_lock = threading.Lock()
_wake_attempted = {"at": 0.0}


class ColdStartError(Exception):
    """Raised when the Lakebase instance did not resume within the wait budget."""


def _client() -> WorkspaceClient:
    global _w
    if _w is None:
        _w = WorkspaceClient()
    return _w


def _host() -> str:
    h = os.getenv("PGHOST")
    if h:
        return h
    if "host" not in _host_cache:
        inst = _client().database.get_database_instance(name=INSTANCE_NAME)
        _host_cache["host"] = inst.read_write_dns
    return _host_cache["host"]


def _user() -> str:
    return (
        os.getenv("PGUSER")
        or os.getenv("DATABRICKS_CLIENT_ID")
        or _client().current_user.me().user_name
    )


def _password() -> str:
    """Fresh OAuth token, cached for 45 minutes (tokens expire after ~1h)."""
    now = time.time()
    with _lock:
        if _token["value"] and now < _token["exp"]:
            return _token["value"]
        cred = _client().database.generate_database_credential(
            request_id=str(uuid.uuid4()), instance_names=[INSTANCE_NAME]
        )
        _token["value"] = cred.token
        _token["exp"] = now + 45 * 60
        return _token["value"]


def _request_wake() -> None:
    """Ask a scale-to-zero instance to start. Best-effort, throttled to once/60s.

    Not all SDK versions expose a start API; failures here are non-fatal because
    a plain connection attempt also triggers the instance to resume.
    """
    now = time.time()
    if now - _wake_attempted["at"] < 60:
        return
    _wake_attempted["at"] = now
    db_api = _client().database
    for method in ("start_database_instance", "wake_database_instance"):
        fn = getattr(db_api, method, None)
        if fn is not None:
            try:
                fn(name=INSTANCE_NAME)
            except Exception:
                pass
            return


def _connect_once():
    return psycopg.connect(
        host=_host(),
        dbname=DB_NAME,
        user=_user(),
        password=_password(),
        port=DB_PORT,
        sslmode="require",
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT,
    )


@contextmanager
def get_conn():
    """Yield a connection, retrying through a scale-to-zero cold start.

    On connection failure we mint a fresh token, nudge the instance to wake, and
    retry with capped exponential backoff until CONNECT_MAX_WAIT elapses. If it
    still isn't up, raise ColdStartError so the UI can show a "waking" message.
    """
    deadline = time.time() + CONNECT_MAX_WAIT
    delay = 1.0
    last_err: Exception | None = None
    conn = None
    while True:
        try:
            conn = _connect_once()
            break
        except psycopg.OperationalError as e:
            last_err = e
            if time.time() >= deadline:
                raise ColdStartError(
                    "The auction database is waking up from idle. "
                    "Please wait a few seconds and try again."
                ) from e
            # Token may have gone stale; force a refresh on the next attempt.
            with _lock:
                _token["exp"] = 0.0
            _request_wake()
            time.sleep(min(delay, max(0.0, deadline - time.time())))
            delay = min(delay * 2, 8.0)
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS auction;

CREATE TABLE IF NOT EXISTS auction.batches (
    batch_id      BIGSERIAL PRIMARY KEY,
    batch_number  INTEGER      NOT NULL,
    label         TEXT,
    uploaded_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    end_time      TIMESTAMPTZ  NOT NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS auction.bidders (
    bidder_id   BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auction.items (
    item_id       BIGSERIAL PRIMARY KEY,
    batch_id      BIGINT NOT NULL REFERENCES auction.batches(batch_id),
    item_number   INTEGER NOT NULL,
    item_name     TEXT NOT NULL,
    starting_bid  NUMERIC(14,2) NOT NULL,
    UNIQUE (batch_id, item_number)
);

CREATE TABLE IF NOT EXISTS auction.bids (
    bid_id      BIGSERIAL PRIMARY KEY,
    item_id     BIGINT NOT NULL REFERENCES auction.items(item_id),
    bidder_id   BIGINT NOT NULL REFERENCES auction.bidders(bidder_id),
    amount      NUMERIC(14,2) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_items_batch ON auction.items(batch_id);
CREATE INDEX IF NOT EXISTS idx_bids_item ON auction.bids(item_id);
"""


def init_schema() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()


# --------------------------------------------------------------------------- #
# Bidders
# --------------------------------------------------------------------------- #
def upsert_bidder(name: str, phone: str, email: str) -> int:
    """Register or re-identify a bidder by email. Returns bidder_id."""
    email = email.strip().lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auction.bidders (name, phone, email)
                VALUES (%s, %s, %s)
                ON CONFLICT (email)
                DO UPDATE SET name = EXCLUDED.name, phone = EXCLUDED.phone
                RETURNING bidder_id
                """,
                (name.strip(), phone.strip(), email),
            )
            bidder_id = cur.fetchone()["bidder_id"]
        conn.commit()
    return bidder_id


# --------------------------------------------------------------------------- #
# Batches
# --------------------------------------------------------------------------- #
def get_active_batch() -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM auction.batches WHERE is_active ORDER BY batch_number DESC LIMIT 1"
            )
            return cur.fetchone()


def list_batches() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.*,
                       (SELECT count(*) FROM auction.items i WHERE i.batch_id = b.batch_id) AS n_items,
                       (SELECT count(*) FROM auction.bids bd
                          JOIN auction.items i ON i.item_id = bd.item_id
                          WHERE i.batch_id = b.batch_id) AS n_bids
                FROM auction.batches b
                ORDER BY b.batch_number DESC
                """
            )
            return cur.fetchall()


def activate_new_batch(items: list[tuple], end_time, label: str | None = None) -> dict:
    """Archive current batches and create a new active one with the given items.

    items: list of (item_number, item_name, starting_bid)
    Previous batches (and their bids) are preserved, just marked inactive.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(batch_number), 0) AS m FROM auction.batches")
            next_number = cur.fetchone()["m"] + 1

            cur.execute("UPDATE auction.batches SET is_active = FALSE WHERE is_active")

            cur.execute(
                """
                INSERT INTO auction.batches (batch_number, label, end_time, is_active)
                VALUES (%s, %s, %s, TRUE)
                RETURNING batch_id, batch_number
                """,
                (next_number, label or f"Batch {next_number}", end_time),
            )
            row = cur.fetchone()
            batch_id = row["batch_id"]

            cur.executemany(
                """
                INSERT INTO auction.items (batch_id, item_number, item_name, starting_bid)
                VALUES (%s, %s, %s, %s)
                """,
                [(batch_id, n, name, Decimal(str(p))) for (n, name, p) in items],
            )
        conn.commit()
    return {"batch_id": batch_id, "batch_number": next_number, "n_items": len(items)}


# --------------------------------------------------------------------------- #
# Items + bids (read)
# --------------------------------------------------------------------------- #
def get_items_with_highest(batch_id: int) -> list[dict]:
    """Items in a batch with current highest bid + bid count (anonymous)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.item_id, i.item_number, i.item_name, i.starting_bid,
                       COALESCE(MAX(b.amount), NULL) AS highest_bid,
                       COUNT(b.bid_id) AS n_bids
                FROM auction.items i
                LEFT JOIN auction.bids b ON b.item_id = i.item_id
                WHERE i.batch_id = %s
                GROUP BY i.item_id, i.item_number, i.item_name, i.starting_bid
                ORDER BY i.item_number
                """,
                (batch_id,),
            )
            return cur.fetchall()


def get_my_bids(batch_id: int, bidder_id: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.item_number, i.item_name, b.amount, b.created_at,
                       (b.amount = (SELECT MAX(b2.amount) FROM auction.bids b2
                                    WHERE b2.item_id = b.item_id)) AS is_leading
                FROM auction.bids b
                JOIN auction.items i ON i.item_id = b.item_id
                WHERE i.batch_id = %s AND b.bidder_id = %s
                ORDER BY b.created_at DESC
                """,
                (batch_id, bidder_id),
            )
            return cur.fetchall()


class BidError(Exception):
    pass


def place_bid(item_id: int, bidder_id: int, amount) -> dict:
    """Place a bid. Must strictly exceed current highest (>= starting price if none).

    Uses SELECT ... FOR UPDATE on the item row to serialize concurrent bids on
    the same item, so two people can't both "win" a race.
    """
    amount = Decimal(str(amount))
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # Lock the item row + fetch its batch end time.
                cur.execute(
                    """
                    SELECT i.starting_bid, b.end_time
                    FROM auction.items i
                    JOIN auction.batches b ON b.batch_id = i.batch_id
                    WHERE i.item_id = %s
                    FOR UPDATE OF i
                    """,
                    (item_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise BidError("Item not found.")

                cur.execute("SELECT now() AS now")
                now = cur.fetchone()["now"]
                if now >= row["end_time"]:
                    raise BidError("Auction has ended — bidding is closed.")

                cur.execute(
                    "SELECT MAX(amount) AS hi FROM auction.bids WHERE item_id = %s",
                    (item_id,),
                )
                highest = cur.fetchone()["hi"]
                floor = highest if highest is not None else row["starting_bid"]

                if highest is None:
                    if amount < floor:
                        raise BidError(
                            f"Bid must be at least the starting price of {floor}."
                        )
                else:
                    if amount <= highest:
                        raise BidError(
                            f"Bid must be greater than the current highest bid of {highest}."
                        )

                cur.execute(
                    """
                    INSERT INTO auction.bids (item_id, bidder_id, amount)
                    VALUES (%s, %s, %s)
                    RETURNING bid_id
                    """,
                    (item_id, bidder_id, amount),
                )
                bid_id = cur.fetchone()["bid_id"]
            conn.commit()
            return {"bid_id": bid_id, "amount": amount}
        except Exception:
            conn.rollback()
            raise
