from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine


load_dotenv()

metadata = MetaData()

upload_batches = Table(
    "upload_batches",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
    Column("uploader_name", String(100), nullable=True),
    Column("file_name", Text, nullable=True),
    Column("file_count", Integer, nullable=False, default=0),
    Column("status", String(20), nullable=False),
    Column("message", Text, nullable=True),
)

daily_metrics = Table(
    "daily_metrics",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("date", Date, nullable=False),
    Column("brand", String(50), nullable=False),
    Column("platform", String(50), nullable=False),
    Column("channel", String(100), nullable=False),
    Column("gmv", Float, nullable=True),
    Column("net_gmv", Float, nullable=True),
    Column("orders", Float, nullable=True),
    Column("ad_spend", Float, nullable=True),
    Column("roi", Float, nullable=True),
    Column("net_roi", Float, nullable=True),
    Column("refund_rate", Float, nullable=True),
    Column("refund_amount", Float, nullable=True),
    Column("conversion_rate", Float, nullable=True),
    Column("click_to_order_rate", Float, nullable=True),
    Column("commission", Float, nullable=True),
    Column("source_file", Text, nullable=True),
    Column("source_sheet", Text, nullable=True),
    Column("upload_batch_id", Integer, ForeignKey("upload_batches.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("date", "brand", "platform", "channel", name="uq_daily_metric_key"),
)

raw_metrics = Table(
    "raw_metrics",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("date", Date, nullable=False),
    Column("brand", String(50), nullable=False),
    Column("platform", String(50), nullable=False),
    Column("channel", String(100), nullable=False),
    Column("metric", Text, nullable=False),
    Column("value", Float, nullable=True),
    Column("source_file", Text, nullable=True),
    Column("source_sheet", Text, nullable=True),
    Column("upload_batch_id", Integer, ForeignKey("upload_batches.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

import_errors = Table(
    "import_errors",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("upload_batch_id", Integer, ForeignKey("upload_batches.id"), nullable=True),
    Column("source_file", Text, nullable=True),
    Column("source_sheet", Text, nullable=True),
    Column("row_info", Text, nullable=True),
    Column("error_message", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

DAILY_METRIC_COLUMNS = [
    "gmv",
    "net_gmv",
    "orders",
    "ad_spend",
    "roi",
    "net_roi",
    "refund_rate",
    "refund_amount",
    "conversion_rate",
    "click_to_order_rate",
    "commission",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_database_url() -> str:
    secret_url = ""
    try:
        secret_url = st.secrets.get("DATABASE_URL", "")
    except Exception:
        secret_url = ""
    database_url = secret_url or os.getenv("DATABASE_URL", "")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    return database_url


@st.cache_resource(show_spinner=False)
def get_engine() -> Engine | None:
    database_url = get_database_url()
    if not database_url:
        return None
    return create_engine(database_url, pool_pre_ping=True, future=True)


def init_db(engine: Engine | None = None) -> bool:
    engine = engine or get_engine()
    if engine is None:
        return False
    metadata.create_all(engine)
    return True


def create_upload_batch(
    file_names: list[str],
    uploader_name: str | None = None,
    status: str = "success",
    message: str = "",
    engine: Engine | None = None,
) -> int | None:
    engine = engine or get_engine()
    if engine is None:
        return None
    payload = {
        "uploaded_at": now_utc(),
        "uploader_name": uploader_name or None,
        "file_name": "；".join(file_names),
        "file_count": len(file_names),
        "status": status,
        "message": message,
    }
    with engine.begin() as conn:
        result = conn.execute(insert(upload_batches).values(**payload))
        return int(result.inserted_primary_key[0])


def update_upload_batch(
    upload_batch_id: int,
    status: str,
    message: str,
    engine: Engine | None = None,
) -> None:
    engine = engine or get_engine()
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(
            update(upload_batches)
            .where(upload_batches.c.id == upload_batch_id)
            .values(status=status, message=message)
        )


def normalize_date(value):
    if pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def normalize_float(value):
    if pd.isna(value):
        return None
    return float(value)


def source_summary(raw_df: pd.DataFrame) -> pd.DataFrame:
    if raw_df.empty:
        return pd.DataFrame(columns=["date", "brand", "platform", "channel", "source_file", "source_sheet"])
    summary = (
        raw_df.groupby(["date", "brand", "platform", "channel"], dropna=False)
        .agg(
            source_file=("source_file", lambda values: "；".join(sorted({str(v) for v in values if pd.notna(v)}))),
            source_sheet=("source_sheet", lambda values: "；".join(sorted({str(v) for v in values if pd.notna(v)}))),
        )
        .reset_index()
    )
    return summary


def upsert_daily_metrics(
    daily_df: pd.DataFrame,
    upload_batch_id: int | None,
    raw_df: pd.DataFrame | None = None,
    engine: Engine | None = None,
) -> dict[str, int]:
    engine = engine or get_engine()
    if engine is None or daily_df.empty:
        return {"inserted": 0, "updated": 0}

    working = daily_df.copy()
    if raw_df is not None and not raw_df.empty:
        working = working.drop(columns=["source_file", "source_sheet"], errors="ignore")
        summary = source_summary(raw_df)
        working = working.merge(summary, on=["date", "brand", "platform", "channel"], how="left")
    else:
        working["source_file"] = None
        working["source_sheet"] = None

    inserted = 0
    updated = 0
    with engine.begin() as conn:
        for _, row in working.iterrows():
            key = {
                "date": normalize_date(row["date"]),
                "brand": str(row["brand"]),
                "platform": str(row["platform"]),
                "channel": str(row["channel"]),
            }
            metric_values = {col: normalize_float(row.get(col)) for col in DAILY_METRIC_COLUMNS}
            now = now_utc()
            payload = {
                **key,
                **metric_values,
                "source_file": row.get("source_file") if pd.notna(row.get("source_file")) else None,
                "source_sheet": row.get("source_sheet") if pd.notna(row.get("source_sheet")) else None,
                "upload_batch_id": upload_batch_id,
                "updated_at": now,
            }

            existing_id = conn.execute(
                select(daily_metrics.c.id).where(
                    daily_metrics.c.date == key["date"],
                    daily_metrics.c.brand == key["brand"],
                    daily_metrics.c.platform == key["platform"],
                    daily_metrics.c.channel == key["channel"],
                )
            ).scalar_one_or_none()

            if existing_id:
                conn.execute(update(daily_metrics).where(daily_metrics.c.id == existing_id).values(**payload))
                updated += 1
            else:
                conn.execute(insert(daily_metrics).values(**payload, created_at=now))
                inserted += 1
    return {"inserted": inserted, "updated": updated}


def insert_raw_metrics(raw_df: pd.DataFrame, upload_batch_id: int | None, engine: Engine | None = None) -> int:
    engine = engine or get_engine()
    if engine is None or raw_df.empty:
        return 0
    records = []
    created_at = now_utc()
    for _, row in raw_df.iterrows():
        records.append(
            {
                "date": normalize_date(row["date"]),
                "brand": str(row["brand"]),
                "platform": str(row["platform"]),
                "channel": str(row["channel"]),
                "metric": str(row["metric"]),
                "value": normalize_float(row.get("value")),
                "source_file": row.get("source_file") if pd.notna(row.get("source_file")) else None,
                "source_sheet": row.get("source_sheet") if pd.notna(row.get("source_sheet")) else None,
                "upload_batch_id": upload_batch_id,
                "created_at": created_at,
            }
        )
    with engine.begin() as conn:
        conn.execute(insert(raw_metrics), records)
    return len(records)


def insert_import_error(
    upload_batch_id: int | None,
    source_file: str | None,
    source_sheet: str | None,
    row_info: str | None,
    error_message: str,
    engine: Engine | None = None,
) -> None:
    engine = engine or get_engine()
    if engine is None:
        return
    payload = {
        "upload_batch_id": upload_batch_id,
        "source_file": source_file,
        "source_sheet": source_sheet,
        "row_info": row_info,
        "error_message": error_message,
        "created_at": now_utc(),
    }
    with engine.begin() as conn:
        conn.execute(insert(import_errors).values(**payload))


def insert_import_errors(
    warnings: list[str],
    upload_batch_id: int | None,
    engine: Engine | None = None,
) -> int:
    engine = engine or get_engine()
    if engine is None or not warnings:
        return 0
    with engine.begin() as conn:
        conn.execute(
            insert(import_errors),
            [
                {
                    "upload_batch_id": upload_batch_id,
                    "source_file": None,
                    "source_sheet": None,
                    "row_info": None,
                    "error_message": warning,
                    "created_at": now_utc(),
                }
                for warning in warnings
            ],
        )
    return len(warnings)


def load_daily_metrics(engine: Engine | None = None) -> pd.DataFrame:
    engine = engine or get_engine()
    columns = [column.name for column in daily_metrics.columns]
    if engine is None:
        return pd.DataFrame(columns=columns)
    with engine.connect() as conn:
        df = pd.read_sql(select(daily_metrics).order_by(daily_metrics.c.date), conn)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def clear_batch_raw_metrics(upload_batch_id: int, engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(delete(raw_metrics).where(raw_metrics.c.upload_batch_id == upload_batch_id))
