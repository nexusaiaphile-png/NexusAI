"""POS-first transaction correlation engine for NexusAI.

POS exception events are the source of truth. Video is attached only by explicit
camera/timeline references; this module does not classify generic camera motion
as theft or any other crime.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .models import POSExceptionBatchIn, POSExceptionEventIn

LOG = logging.getLogger("nexusai.transaction_correlation")
BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("NEXUSAI_DB_PATH", str(BASE_DIR / "nexusai.db")))
_DB_LOCK = threading.RLock()
_REPORT_WORKER: threading.Thread | None = None
_REPORT_STOP = threading.Event()


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _connect():
    if _database_url():
        import psycopg
        return psycopg.connect(_database_url())
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    # SQLite disables FK enforcement by default; enable it on every connection.
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _execute(conn, sql: str, params: tuple = ()):
    if _database_url():
        # Keep the cursor open until the caller fetches; the connection context
        # owns cleanup and is closed in the caller's finally block.
        cur = conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur
    return conn.execute(sql, params)


def init_schema() -> None:
    """Create additive tables; existing security tables and data remain untouched."""
    id_type = "BIGSERIAL PRIMARY KEY" if _database_url() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statements = [
        f"""CREATE TABLE IF NOT EXISTS nexusai_pos_exceptions (
            id {id_type},
            site_id TEXT NOT NULL,
            till_id TEXT NOT NULL,
            exception_type TEXT NOT NULL,
            transaction_id TEXT,
            cashier_id TEXT,
            cashier_name TEXT,
            transaction_value NUMERIC(14,2) NOT NULL DEFAULT 0,
            amount NUMERIC(14,2) NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'ZAR',
            occurred_at TEXT NOT NULL,
            active_transaction BOOLEAN,
            flagged_for_review BOOLEAN NOT NULL DEFAULT TRUE,
            camera_id TEXT,
            clip_start TEXT,
            clip_end TEXT,
            video_clip_url TEXT,
            source_event_id TEXT,
            correlation_status TEXT NOT NULL DEFAULT 'pending_video_reference',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(site_id, source_event_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS nexusai_video_timeline_refs (
            id {id_type},
            site_id TEXT NOT NULL,
            camera_id TEXT NOT NULL,
            clip_start TEXT NOT NULL,
            clip_end TEXT NOT NULL,
            video_clip_url TEXT NOT NULL,
            source_ref_id TEXT,
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(site_id, source_ref_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS nexusai_pos_exception_video_links (
            id {id_type},
            exception_id BIGINT NOT NULL,
            timeline_ref_id BIGINT,
            site_id TEXT NOT NULL,
            camera_id TEXT,
            clip_start TEXT,
            clip_end TEXT,
            video_clip_url TEXT,
            source_ref_id TEXT,
            match_status TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(exception_id),
            CONSTRAINT fk_pos_video_link_exception
                FOREIGN KEY (exception_id) REFERENCES nexusai_pos_exceptions(id) ON DELETE CASCADE,
            CONSTRAINT fk_pos_video_link_timeline
                FOREIGN KEY (timeline_ref_id) REFERENCES nexusai_video_timeline_refs(id) ON DELETE RESTRICT
        )""",
        f"""CREATE TABLE IF NOT EXISTS nexusai_conversion_metrics (
            id {id_type},
            site_id TEXT NOT NULL,
            camera_id TEXT,
            interval_start TEXT NOT NULL,
            interval_end TEXT NOT NULL,
            traffic_count INTEGER NOT NULL,
            invoice_count INTEGER NOT NULL,
            average_basket_value NUMERIC(14,2) NOT NULL DEFAULT 0,
            estimated_conversion_loss NUMERIC(14,2) NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'ZAR',
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(site_id, camera_id, interval_start, interval_end)
        )""",
        f"""CREATE TABLE IF NOT EXISTS nexusai_owner_reports (
            id {id_type},
            site_id TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            report_type TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(site_id, period_start, period_end, report_type)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pos_exceptions_site_time ON nexusai_pos_exceptions(site_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_exception_video_links_site_camera ON nexusai_pos_exception_video_links(site_id, camera_id, clip_start, clip_end)",
        "CREATE INDEX IF NOT EXISTS idx_video_timeline_lookup ON nexusai_video_timeline_refs(site_id, camera_id, clip_start, clip_end)",
        "CREATE INDEX IF NOT EXISTS idx_pos_exceptions_till_time ON nexusai_pos_exceptions(site_id, till_id, occurred_at)",
    ]
    with _DB_LOCK:
        conn = _connect()
        try:
            for statement in statements:
                _execute(conn, statement)
            # Additive migration for databases created by an earlier draft version.
            for column, declaration in (("cashier_id", "TEXT"), ("cashier_name", "TEXT"), ("transaction_value", "NUMERIC(14,2) NOT NULL DEFAULT 0")):
                if _database_url():
                    cur = _execute(conn, "SELECT column_name FROM information_schema.columns WHERE table_name='nexusai_pos_exceptions' AND column_name=?", (column,))
                    exists = cur.fetchone() is not None
                else:
                    cur = _execute(conn, "PRAGMA table_info(nexusai_pos_exceptions)")
                    exists = column in {row[1] for row in cur.fetchall()}
                if not exists:
                    _execute(conn, f"ALTER TABLE nexusai_pos_exceptions ADD COLUMN {column} {declaration}")

            # Refuse to hide or silently delete pre-existing orphaned relations.
            orphan_exception = _execute(conn, """SELECT l.id FROM nexusai_pos_exception_video_links l
                LEFT JOIN nexusai_pos_exceptions e ON e.id=l.exception_id
                WHERE e.id IS NULL LIMIT 1""").fetchone()
            orphan_timeline = _execute(conn, """SELECT l.id FROM nexusai_pos_exception_video_links l
                LEFT JOIN nexusai_video_timeline_refs v ON v.id=l.timeline_ref_id
                WHERE l.timeline_ref_id IS NOT NULL AND v.id IS NULL LIMIT 1""").fetchone()
            if orphan_exception or orphan_timeline:
                which = "exception_id" if orphan_exception else "timeline_ref_id"
                bad_id = (orphan_exception or orphan_timeline)[0]
                raise RuntimeError(
                    f"Foreign-key migration blocked: junction row {bad_id} has an orphan {which}; "
                    "repair the referenced data before restarting. No rows were deleted."
                )

            if _database_url():
                # PostgreSQL: add constraints to an existing draft table as well as new tables.
                existing_constraints = {
                    row[0] for row in _execute(conn, """SELECT conname FROM pg_constraint
                        WHERE conrelid='nexusai_pos_exception_video_links'::regclass""").fetchall()
                }
                if "fk_pos_video_link_exception" not in existing_constraints:
                    _execute(conn, """ALTER TABLE nexusai_pos_exception_video_links
                        ADD CONSTRAINT fk_pos_video_link_exception
                        FOREIGN KEY (exception_id) REFERENCES nexusai_pos_exceptions(id) ON DELETE CASCADE""")
                if "fk_pos_video_link_timeline" not in existing_constraints:
                    _execute(conn, """ALTER TABLE nexusai_pos_exception_video_links
                        ADD CONSTRAINT fk_pos_video_link_timeline
                        FOREIGN KEY (timeline_ref_id) REFERENCES nexusai_video_timeline_refs(id) ON DELETE RESTRICT""")
            else:
                fk_rows = _execute(conn, "PRAGMA foreign_key_list(nexusai_pos_exception_video_links)").fetchall()
                fk_targets = {(row[2], row[3], row[4]) for row in fk_rows}
                required_fks = {
                    ("nexusai_pos_exceptions", "exception_id", "id"),
                    ("nexusai_video_timeline_refs", "timeline_ref_id", "id"),
                }
                if not required_fks.issubset(fk_targets):
                    # SQLite cannot ALTER TABLE to add foreign keys. Rebuild safely after
                    # checking all existing rows; preserve every row and recreate the index.
                    _execute(conn, "DROP INDEX IF EXISTS idx_exception_video_links_site_camera")
                    _execute(conn, "ALTER TABLE nexusai_pos_exception_video_links RENAME TO nexusai_pos_exception_video_links_legacy")
                    _execute(conn, """CREATE TABLE nexusai_pos_exception_video_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        exception_id BIGINT NOT NULL,
                        timeline_ref_id BIGINT,
                        site_id TEXT NOT NULL,
                        camera_id TEXT,
                        clip_start TEXT,
                        clip_end TEXT,
                        video_clip_url TEXT,
                        source_ref_id TEXT,
                        match_status TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        UNIQUE(exception_id),
                        CONSTRAINT fk_pos_video_link_exception
                            FOREIGN KEY (exception_id) REFERENCES nexusai_pos_exceptions(id) ON DELETE CASCADE,
                        CONSTRAINT fk_pos_video_link_timeline
                            FOREIGN KEY (timeline_ref_id) REFERENCES nexusai_video_timeline_refs(id) ON DELETE RESTRICT
                    )""")
                    _execute(conn, """INSERT INTO nexusai_pos_exception_video_links
                        (id,exception_id,timeline_ref_id,site_id,camera_id,clip_start,clip_end,
                         video_clip_url,source_ref_id,match_status,created_at)
                        SELECT id,exception_id,timeline_ref_id,site_id,camera_id,clip_start,clip_end,
                         video_clip_url,source_ref_id,match_status,created_at
                        FROM nexusai_pos_exception_video_links_legacy""")
                    _execute(conn, "DROP TABLE nexusai_pos_exception_video_links_legacy")
                    _execute(conn, """CREATE INDEX IF NOT EXISTS idx_exception_video_links_site_camera
                        ON nexusai_pos_exception_video_links(site_id, camera_id, clip_start, clip_end)""")
            conn.commit()
        finally:
            conn.close()


def _admin_guard(key: str | None) -> None:
    expected = os.getenv("NEXUSAI_ADMIN_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="NexusAI admin key is not configured.")
    if not key or not __import__("secrets").compare_digest(key.strip(), expected):
        raise HTTPException(status_code=401, detail="Invalid NexusAI admin key.")


def _iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Timestamp must be ISO-8601.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CashierIn(BaseModel):
    id: str | None = Field(default=None, max_length=160)
    name: str | None = Field(default=None, max_length=200)


class ExceptionVideoTimelineIn(BaseModel):
    camera_id: str | None = Field(default=None, max_length=128)
    clip_start: str | None = Field(default=None, max_length=80)
    clip_end: str | None = Field(default=None, max_length=80)
    video_clip_url: str | None = Field(default=None, max_length=2048)
    source_ref_id: str | None = Field(default=None, max_length=200)


class TillExceptionIn(BaseModel):
    site_id: str = Field(min_length=8, max_length=100)
    till_id: str = Field(min_length=1, max_length=128)
    exception_type: Literal["refund", "return", "return_issued", "no_sale", "drawer_open", "void", "transaction_void", "line_item_cancelled", "manager_override", "manual_discount"]
    transaction_id: str | None = Field(default=None, max_length=160)
    cashier: CashierIn = Field(default_factory=CashierIn)
    transaction_value: float = Field(default=0, ge=0, le=100000000)
    # Backward compatibility for existing POS adapters; transaction_value is canonical.
    amount: float | None = Field(default=None, ge=0, le=100000000, exclude=True)
    currency: str = Field(default="ZAR", min_length=3, max_length=3)
    occurred_at: str = Field(min_length=10, max_length=80)
    active_transaction: bool | None = None
    camera_id: str | None = Field(default=None, max_length=128)
    clip_start: str | None = Field(default=None, max_length=80)
    clip_end: str | None = Field(default=None, max_length=80)
    video_clip_url: str | None = Field(default=None, max_length=2048)
    video_timeline: ExceptionVideoTimelineIn | None = None
    source_event_id: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_and_nested_fields(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "transaction_value" not in data and data.get("amount") is not None:
            data["transaction_value"] = data["amount"]
        if "amount" not in data and "transaction_value" in data:
            data["amount"] = data["transaction_value"]
        nested = data.get("video_timeline")
        if isinstance(nested, dict):
            for target in ("camera_id", "clip_start", "clip_end", "video_clip_url"):
                if not data.get(target) and nested.get(target):
                    data[target] = nested[target]
        return data

    @model_validator(mode="after")
    def keep_amount_alias_in_sync(self):
        if self.amount is not None and self.transaction_value == 0:
            self.transaction_value = self.amount
        self.amount = self.transaction_value
        return self

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: str) -> str:
        return _iso(value)

    @field_validator("clip_start", "clip_end")
    @classmethod
    def validate_clip_time(cls, value: str | None) -> str | None:
        return _iso(value) if value else value

    @field_validator("video_clip_url")
    @classmethod
    def validate_clip_url(cls, value: str | None) -> str | None:
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("video_clip_url must be an HTTPS URL.")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        return value.upper()


class VideoTimelineIn(BaseModel):
    site_id: str = Field(min_length=8, max_length=100)
    camera_id: str = Field(min_length=1, max_length=128)
    clip_start: str = Field(min_length=10, max_length=80)
    clip_end: str = Field(min_length=10, max_length=80)
    video_clip_url: str = Field(min_length=10, max_length=2048)
    source_ref_id: str | None = Field(default=None, max_length=200)

    @field_validator("clip_start", "clip_end")
    @classmethod
    def validate_clip_times(cls, value: str) -> str:
        return _iso(value)

    @field_validator("video_clip_url")
    @classmethod
    def validate_video_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("video_clip_url must be an HTTPS URL.")
        return value


class ConversionMetricIn(BaseModel):
    site_id: str = Field(min_length=8, max_length=100)
    camera_id: str | None = Field(default=None, max_length=128)
    interval_start: str = Field(min_length=10, max_length=80)
    interval_end: str = Field(min_length=10, max_length=80)
    traffic_count: int = Field(ge=0, le=10000000)
    invoice_count: int = Field(ge=0, le=10000000)
    average_basket_value: float = Field(default=0, ge=0, le=100000000)
    currency: str = Field(default="ZAR", min_length=3, max_length=3)

    @field_validator("interval_start", "interval_end")
    @classmethod
    def validate_times(cls, value: str) -> str:
        return _iso(value)


def _insert_exception(event: TillExceptionIn) -> dict[str, Any]:
    now = time.time()
    # Prefer an explicitly supplied clip reference; otherwise match an ingested
    # camera timeline whose interval contains the POS event timestamp.
    if event.camera_id and not (event.clip_start and event.clip_end and event.video_clip_url):
        with _DB_LOCK:
            conn = _connect()
            try:
                cur = _execute(conn, """SELECT clip_start, clip_end, video_clip_url
                    FROM nexusai_video_timeline_refs
                    WHERE site_id=? AND camera_id=? AND clip_start<=? AND clip_end>=?
                    ORDER BY clip_start DESC LIMIT 1""",
                    (event.site_id, event.camera_id, event.occurred_at, event.occurred_at))
                clip = cur.fetchone()
                if clip:
                    event.clip_start, event.clip_end, event.video_clip_url = clip[0], clip[1], clip[2]
            finally:
                conn.close()
    # A till event is flagged from POS semantics, not inferred from video.
    needs_review = event.exception_type in {"refund", "return", "return_issued", "no_sale", "void", "transaction_void", "line_item_cancelled", "manager_override", "manual_discount"} or (
        event.exception_type == "drawer_open" and event.active_transaction is not True
    )
    correlation_status = "matched_video_timeline" if (
        event.camera_id and event.clip_start and event.clip_end and event.video_clip_url
    ) else "pending_video_reference"
    metadata = json.dumps(event.metadata, separators=(",", ":"), default=str)
    sql = """INSERT INTO nexusai_pos_exceptions
        (site_id,till_id,exception_type,transaction_id,cashier_id,cashier_name,transaction_value,amount,currency,occurred_at,
         active_transaction,flagged_for_review,camera_id,clip_start,clip_end,video_clip_url,source_event_id,
         correlation_status,metadata_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(site_id, source_event_id) DO NOTHING"""
    params = (
        event.site_id, event.till_id, event.exception_type, event.transaction_id,
        event.cashier.id, event.cashier.name, event.transaction_value, event.transaction_value,
        event.currency, event.occurred_at, event.active_transaction,
        needs_review, event.camera_id, event.clip_start, event.clip_end, event.video_clip_url,
        event.source_event_id, correlation_status, metadata, now,
    )
    with _DB_LOCK:
        conn = _connect()
        try:
            _execute(conn, sql, params)
            conn.commit()
            # Re-read by idempotency key or newest event when no source id was supplied.
            if event.source_event_id:
                cur = _execute(conn, "SELECT id FROM nexusai_pos_exceptions WHERE site_id=? AND source_event_id=?",
                               (event.site_id, event.source_event_id))
            else:
                cur = _execute(conn, "SELECT id FROM nexusai_pos_exceptions WHERE site_id=? AND till_id=? AND occurred_at=? ORDER BY id DESC LIMIT 1",
                               (event.site_id, event.till_id, event.occurred_at))
            row = cur.fetchone()
            event_id = row[0] if row else None
        finally:
            conn.close()

    # Store a relational, traceable link to the exact video timeline used for this exception.
    if event_id is not None:
        with _DB_LOCK:
            conn = _connect()
            try:
                timeline_ref_id = None
                if event.video_timeline and event.video_timeline.source_ref_id:
                    cur = _execute(conn, "SELECT id FROM nexusai_video_timeline_refs WHERE site_id=? AND source_ref_id=?", (event.site_id, event.video_timeline.source_ref_id))
                    found = cur.fetchone()
                    timeline_ref_id = found[0] if found else None
                link_sql = """INSERT INTO nexusai_pos_exception_video_links
                    (exception_id,timeline_ref_id,site_id,camera_id,clip_start,clip_end,video_clip_url,source_ref_id,match_status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(exception_id) DO UPDATE SET
                    timeline_ref_id=excluded.timeline_ref_id,camera_id=excluded.camera_id,
                    clip_start=excluded.clip_start,clip_end=excluded.clip_end,video_clip_url=excluded.video_clip_url,
                    source_ref_id=excluded.source_ref_id,match_status=excluded.match_status,created_at=excluded.created_at"""
                _execute(conn, link_sql, (event_id, timeline_ref_id, event.site_id, event.camera_id,
                    event.clip_start, event.clip_end, event.video_clip_url,
                    event.video_timeline.source_ref_id if event.video_timeline else None,
                    correlation_status, now))
                conn.commit()
            finally:
                conn.close()

    # Aggregate monetary exception volume for this till over the preceding 2 hours.
    end = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    start = (end - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    with _DB_LOCK:
        conn = _connect()
        try:
            cur = _execute(conn, """SELECT COUNT(*), COALESCE(SUM(amount),0)
                FROM nexusai_pos_exceptions
                WHERE site_id=? AND till_id=? AND occurred_at>=? AND occurred_at<=?
                AND flagged_for_review=TRUE AND exception_type IN ('refund','return','return_issued','no_sale','void','transaction_void','line_item_cancelled','manager_override','manual_discount')""",
                (event.site_id, event.till_id, start, event.occurred_at))
            aggregate = cur.fetchone()
        finally:
            conn.close()
    return {
        "exception_id": event_id,
        "flagged_for_review": needs_review,
        "till_void_exception": {
            "exception_type": event.exception_type,
            "transaction_id": event.transaction_id,
            "cashier": {"id": event.cashier.id, "name": event.cashier.name},
            "transaction_value": event.transaction_value,
            "active_transaction": event.active_transaction,
            "flagged_for_review": needs_review,
        },
        "correlation_status": correlation_status,
        "camera_id": event.camera_id,
        "video_timeline": {
            "camera_id": event.camera_id,
            "clip_start": event.clip_start, "clip_end": event.clip_end,
            "video_clip_url": event.video_clip_url,
            "source_ref_id": event.video_timeline.source_ref_id if event.video_timeline else None,
        } if correlation_status == "matched_video_timeline" else None,
        "shrinkage_analytics": {
            "site_id": event.site_id, "till_id": event.till_id,
            "window_start": start, "window_end": event.occurred_at,
            "exception_count": int(aggregate[0] or 0),
            "amount_total": float(aggregate[1] or 0),
            "currency": event.currency,
            "footage_url": event.video_clip_url,
        },
    }


def _upsert_conversion_metric(metric: ConversionMetricIn) -> dict[str, Any]:
    if metric.interval_end <= metric.interval_start:
        raise HTTPException(status_code=422, detail="interval_end must be after interval_start.")
    # This is a directional estimate, not proof that each non-converting visitor abandoned a queue.
    gap = max(0, metric.traffic_count - metric.invoice_count)
    estimated_loss = round(gap * metric.average_basket_value, 2)
    sql = """INSERT INTO nexusai_conversion_metrics
        (site_id,camera_id,interval_start,interval_end,traffic_count,invoice_count,
         average_basket_value,estimated_conversion_loss,currency,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(site_id, camera_id, interval_start, interval_end) DO UPDATE SET
        traffic_count=excluded.traffic_count, invoice_count=excluded.invoice_count,
        average_basket_value=excluded.average_basket_value,
        estimated_conversion_loss=excluded.estimated_conversion_loss,
        currency=excluded.currency, created_at=excluded.created_at"""
    with _DB_LOCK:
        conn = _connect()
        try:
            _execute(conn, sql, (metric.site_id, metric.camera_id, metric.interval_start,
                metric.interval_end, metric.traffic_count, metric.invoice_count,
                metric.average_basket_value, estimated_loss, metric.currency.upper(), time.time()))
            conn.commit()
        finally:
            conn.close()
    return {
        "conversion_loss": {
        "site_id": metric.site_id, "camera_id": metric.camera_id,
        "interval_start": metric.interval_start, "interval_end": metric.interval_end,
        "traffic_count": metric.traffic_count, "invoice_count": metric.invoice_count,
        "conversion_gap": gap, "estimated_conversion_loss": estimated_loss,
        "currency": metric.currency.upper(),
        "estimate_note": "Directional estimate; traffic and invoices must use aligned intervals and calibrated counting.",
        },
    }


def _weekly_summary(site_id: str, start: str, end: str) -> dict[str, Any]:
    with _DB_LOCK:
        conn = _connect()
        try:
            cur = _execute(conn, """SELECT COUNT(*), COUNT(DISTINCT till_id),
                COALESCE(SUM(amount),0), COUNT(DISTINCT transaction_id)
                FROM nexusai_pos_exceptions WHERE site_id=? AND occurred_at>=? AND occurred_at<?
                AND flagged_for_review=TRUE AND exception_type IN ('refund','return','return_issued','no_sale','drawer_open','void','transaction_void','line_item_cancelled','manager_override','manual_discount')""",
                (site_id, start, end))
            row = cur.fetchone()
            cur2 = _execute(conn, """SELECT camera_id, interval_start, interval_end,
                traffic_count, invoice_count, estimated_conversion_loss, currency
                FROM nexusai_conversion_metrics WHERE site_id=? AND interval_start>=? AND interval_end<=?
                ORDER BY interval_start""", (site_id, start, end))
            conversion = [
                {"camera_id": r[0], "interval_start": r[1], "interval_end": r[2],
                 "traffic_count": r[3], "invoice_count": r[4],
                 "estimated_conversion_loss": float(r[5] or 0), "currency": r[6]}
                for r in cur2.fetchall()
            ]
        finally:
            conn.close()
    return {
        "site_id": site_id, "period_start": start, "period_end": end,
        "exception_count": int(row[0] or 0), "tills_affected": int(row[1] or 0),
        "exception_amount_total": float(row[2] or 0),
        "transactions_identified": int(row[3] or 0),
        "conversion_intervals": conversion,
        "report_type": "exception_reporting",
        "exception_reporting": {"exception_count": int(row[0] or 0), "tills_affected": int(row[1] or 0)},
    }


def _store_weekly_reports() -> None:
    now = datetime.now(timezone.utc)
    # Previous complete Monday-to-Monday UTC reporting period.
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = this_monday - timedelta(days=7)
    start, end = start_dt.isoformat().replace("+00:00", "Z"), this_monday.isoformat().replace("+00:00", "Z")
    with _DB_LOCK:
        conn = _connect()
        try:
            cur = _execute(conn, "SELECT DISTINCT site_id FROM nexusai_pos_exceptions WHERE occurred_at>=? AND occurred_at<?",
                           (start, end))
            sites = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    for site_id in sites:
        summary = _weekly_summary(site_id, start, end)
        with _DB_LOCK:
            conn = _connect()
            try:
                _execute(conn, """INSERT INTO nexusai_owner_reports
                    (site_id,period_start,period_end,report_type,summary_json,created_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(site_id,period_start,period_end,report_type)
                    DO UPDATE SET summary_json=excluded.summary_json, created_at=excluded.created_at""",
                    (site_id, start, end, "weekly_transaction_correlation",
                     json.dumps(summary, separators=(",", ":")), time.time()))
                conn.commit()
            finally:
                conn.close()
    LOG.info("Weekly transaction correlation reports stored: sites=%d period=%s..%s", len(sites), start, end)


def _report_loop() -> None:
    # Persist a report on startup and then every 24 hours; the weekly period is idempotent.
    while not _REPORT_STOP.is_set():
        try:
            _store_weekly_reports()
        except Exception:
            LOG.exception("Weekly transaction correlation report job failed.")
        _REPORT_STOP.wait(max(3600, int(os.getenv("NEXUSAI_REPORT_REFRESH_SECONDS", "86400"))))


def install_transaction_correlation(app: FastAPI) -> None:
    @app.post("/api/retail/transaction-exceptions")
    async def ingest_transaction_exception(
            payload: POSExceptionEventIn | POSExceptionBatchIn | TillExceptionIn | list[POSExceptionEventIn],
            x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)

        if isinstance(payload, POSExceptionBatchIn):
            incoming = payload.events
            is_batch = True
        elif isinstance(payload, list):
            incoming = payload
            is_batch = True
        else:
            incoming = [payload]
            is_batch = False

        results = []
        try:
            for item in incoming:
                if isinstance(item, POSExceptionEventIn):
                    # Convert the vendor-facing flat cashier fields into the
                    # engine's existing nested representation without changing
                    # the persisted schema or legacy request contract.
                    event = TillExceptionIn.model_validate({
                        "site_id": item.site_id,
                        "source_event_id": item.source_event_id,
                        "till_id": item.till_id,
                        "exception_type": item.exception_type,
                        "transaction_id": item.transaction_id,
                        "cashier": {"id": item.cashier_id, "name": item.cashier_name},
                        "transaction_value": item.transaction_value,
                        "currency": item.currency,
                        "occurred_at": item.occurred_at.isoformat(),
                        "active_transaction": item.active_transaction,
                        "metadata": item.metadata,
                    })
                else:
                    event = item
                results.append(_insert_exception(event))
        except Exception as exc:
            LOG.exception("POS exception ingestion failed.")
            raise HTTPException(status_code=500, detail="Could not persist POS transaction exception.") from exc

        if is_batch:
            return {
                "accepted": True,
                "engine": "transaction_correlation",
                "accepted_count": len(results),
                "results": results,
            }
        return {"accepted": True, "engine": "transaction_correlation", **results[0]}

    @app.post("/api/retail/video-timeline")
    async def ingest_video_timeline(payload: VideoTimelineIn,
            x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        sql = """INSERT INTO nexusai_video_timeline_refs
            (site_id,camera_id,clip_start,clip_end,video_clip_url,source_ref_id,created_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(site_id, source_ref_id) DO UPDATE SET
            camera_id=excluded.camera_id, clip_start=excluded.clip_start,
            clip_end=excluded.clip_end, video_clip_url=excluded.video_clip_url,
            created_at=excluded.created_at"""
        with _DB_LOCK:
            conn = _connect()
            try:
                _execute(conn, sql, (payload.site_id, payload.camera_id, payload.clip_start,
                    payload.clip_end, payload.video_clip_url, payload.source_ref_id, time.time()))
                conn.commit()
            finally:
                conn.close()
        return {"accepted": True, "timeline_reference": payload.model_dump()}

    @app.post("/api/retail/conversion-metrics")
    async def ingest_conversion_metrics(payload: ConversionMetricIn,
            x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        return _upsert_conversion_metric(payload)

    @app.get("/api/retail/owner-dashboard/weekly")
    async def owner_weekly_dashboard(site_id: str, x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        now = datetime.now(timezone.utc)
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        start = (this_monday - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        end = this_monday.isoformat().replace("+00:00", "Z")
        summary = _weekly_summary(site_id, start, end)
        return {"dashboard": summary, "generated_at": datetime.now(timezone.utc).isoformat()}

    @app.get("/api/retail/owner-dashboard/shrinkage")
    async def owner_shrinkage_dashboard(site_id: str, hours: int = 2,
            x_nexusai_admin_key: str | None = Header(default=None)):
        _admin_guard(x_nexusai_admin_key)
        hours = max(1, min(hours, 168))
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=hours)
        start, end = start_dt.isoformat().replace("+00:00", "Z"), end_dt.isoformat().replace("+00:00", "Z")
        with _DB_LOCK:
            conn = _connect()
            try:
                cur = _execute(conn, """SELECT till_id, COUNT(*), COALESCE(SUM(amount),0),
                    MAX(video_clip_url), MAX(currency) FROM nexusai_pos_exceptions
                    WHERE site_id=? AND occurred_at>=? AND occurred_at<?
                    AND flagged_for_review=TRUE AND exception_type IN ('refund','return','return_issued','no_sale','drawer_open','void','transaction_void','line_item_cancelled','manager_override','manual_discount')
                    GROUP BY till_id ORDER BY SUM(amount) DESC""", (site_id, start, end))
                rows = cur.fetchall()
            finally:
                conn.close()
        return {"site_id": site_id, "window_start": start, "window_end": end,
                "tills": [{"till_id": r[0], "exception_count": int(r[1]),
                    "amount_total": float(r[2] or 0), "footage_url": r[3], "currency": r[4]}
                    for r in rows]}

    @app.on_event("startup")
    async def transaction_correlation_startup():
        global _REPORT_WORKER
        init_schema()
        enabled = os.getenv("NEXUSAI_TRANSACTION_REPORT_WORKER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        if enabled and not (_REPORT_WORKER and _REPORT_WORKER.is_alive()):
            _REPORT_STOP.clear()
            _REPORT_WORKER = threading.Thread(target=_report_loop, name="nexusai-weekly-owner-report", daemon=True)
            _REPORT_WORKER.start()
