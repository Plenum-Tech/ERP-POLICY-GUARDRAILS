# app/erp_models.py
from __future__ import annotations

import uuid
from sqlalchemy import (
    MetaData, Table, Column, String, Date, Time, Numeric, DateTime, text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB

# This metadata is only for the erp_ai schema
metadata = MetaData(schema="erp_ai")


def uuid_pk():
    return Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def common_meta():
    return [
        Column("model_id", String, nullable=False),       # e.g., prebuilt-invoice, prebuilt-idDocument
        Column("source_filename", String),
        Column("source_url", String),
        Column("content_type", String),
        Column("created_at", DateTime(timezone=True), server_default=text("now()")),
    ]


# -----------------------------------------------------------
# INVOICE  (from invoice.invoice_to_dict: fields + items[])
# -----------------------------------------------------------
invoice_docs = Table(
    "invoice_docs",
    metadata,
    uuid_pk(),
    *common_meta(),
    # flattened key invoice fields
    Column("vendor_name", String),
    Column("vendor_address", String),
    Column("vendor_address_recipient", String),
    Column("customer_name", String),
    Column("customer_id", String),
    Column("customer_address", String),
    Column("customer_address_recipient", String),
    Column("invoice_id", String),
    Column("invoice_date", Date),
    Column("invoice_total", Numeric),
    Column("due_date", Date),
    Column("purchase_order", String),
    Column("billing_address", String),
    Column("billing_address_recipient", String),
    Column("shipping_address", String),
    Column("shipping_address_recipient", String),
    Column("subtotal", Numeric),
    Column("total_tax", Numeric),
    Column("previous_unpaid_balance", Numeric),
    Column("amount_due", Numeric),
    Column("service_start_date", Date),
    Column("service_end_date", Date),
    Column("service_address", String),
    Column("service_address_recipient", String),
    Column("remittance_address", String),
    Column("remittance_address_recipient", String),
    # full shaped payloads
    Column("fields", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("items", JSONB),  # array of line items
)


# -----------------------------------------------------------
# RECEIPT (from receipt.receipt_to_dict: fields + items[])
# -----------------------------------------------------------
receipt_docs = Table(
    "receipt_docs",
    metadata,
    uuid_pk(),
    *common_meta(),
    # flattened header fields
    Column("merchant_name", String),
    Column("merchant_phone_number", String),
    Column("merchant_address", String),
    Column("merchant_address_recipient", String),
    Column("transaction_date", Date),
    Column("transaction_time", Time),
    Column("total", Numeric),
    Column("subtotal", Numeric),
    Column("total_tax", Numeric),
    Column("tip", Numeric),
    Column("currency", String),
    Column("payment_method", String),
    Column("card_number", String),
    Column("receipt_type", String),
    Column("customer_name", String),
    Column("order_number", String),
    Column("invoice_id", String),
    # full shaped payloads
    Column("fields", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("items", JSONB),  # array of items
)
