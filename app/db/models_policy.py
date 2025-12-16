from sqlalchemy import (
    Table,
    Column,
    String,
    Integer,
    BigInteger,
    DateTime,
    text,
    MetaData,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
import uuid

metadata = MetaData(schema="erp_ai")   # your existing schema

# ------------------------------------------------------------------------
# policy_rulepacks: JSON rule pack per org
# ------------------------------------------------------------------------
policy_rulepacks = Table(
    "policy_rulepacks",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("org_id", String, nullable=False, index=True),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("rulepack", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()")),
)

# ------------------------------------------------------------------------
# policy_audit_log: one row per policy evaluation (for recon / finance)
# ------------------------------------------------------------------------
policy_audit_log = Table(
    "policy_audit_log",
    metadata,
    # DB column is bigint → map to BigInteger autoincrement PK
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("org_id", String, nullable=False, index=True),
    Column("user_id", String, nullable=True),
    Column("status", String, nullable=False),
    Column("risk_score", Integer, nullable=False, server_default="0"),
    Column("risk_flags", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("messages", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("tx", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), server_default=text("now()")),
)


# ------------------------------------------------------------------------
# policy_overrides: captures who/why/when an evaluation was overridden
# ------------------------------------------------------------------------
policy_overrides = Table(
    "policy_overrides",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # Link back to the evaluation in policy_audit_log (optional but recommended)
    Column("audit_id", BigInteger, nullable=True, index=True),
    Column("org_id", String, nullable=False, index=True),
    # The actor performing the override (usually manager/finance)
    Column("user_id", String, nullable=False),
    # Optional hint: MANAGER / FINANCE / ADMIN, etc.
    Column("actor_role", String, nullable=True),
    # Status before override (from policy_audit_log.status)
    Column("original_status", String, nullable=False),
    # Status after override (e.g. OVERRIDDEN_APPROVED / OVERRIDDEN_REJECTED)
    Column("new_status", String, nullable=False),
    # High-level action: APPROVE / REJECT / FORCE_SUBMIT etc.
    Column("action", String, nullable=False),
    # Free-text reason or justification
    Column("reason", Text, nullable=True),
    # Optional JSON blob (extra metadata if you ever want it)
    Column("meta", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), server_default=text("now()")),
)