"""M1 initial schema -- all 32 tables (v0.1 sections A-J, CL-01..CL-26).

Generated from app.db.models metadata (see /tmp/gen_migration.py) and
hand-reviewed; do not edit the table definitions by hand -- change the
models and regenerate, or write a new migration. RLS ENABLE + FORCE is
applied here for every tenant table (and merchants, the tenant root);
the RLS POLICIES themselves live in app/db/rls.sql, applied by the
migrator as a separate reviewed step, as are the triggers in
app/db/triggers.sql and the grants in app/db/roles.sql.

Revision ID: 0001
Revises: (none -- initial migration)
Create Date: 2026-10-31
"""
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('couriers',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('code', sa.Text(), nullable=False, unique=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.CheckConstraint("code IN ('steadfast', 'pathao', 'paperfly', 'redx', 'ecourier')", name='ck_couriers_code'),
    )
    op.create_table('users',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('email', sa.String(320), nullable=False, unique=True),
        sa.Column('display_name', sa.String(200), nullable=True),
        sa.Column('password_hash', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("status IN ('active','invited','suspended','closed')", name='ck_users_status'),
        sa.CheckConstraint("email LIKE '%@%'", name='ck_users_email_has_at'),
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
    op.create_table('courier_statement_templates',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('courier_id', sa.Uuid(), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('header_fingerprint', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=False, unique=True),
        sa.Column('status', sa.Text(), nullable=False, default='draft'),
        sa.Column('evidence_ref', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('version_no > 0', name='ck_templates_version_no'),
        sa.UniqueConstraint('id', name='uq_templates_id'),
        sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name='fk_templates_courier', ondelete='RESTRICT'),
        sa.CheckConstraint("status IN ('draft', 'active', 'retired')", name='ck_templates_status'),
        sa.UniqueConstraint('courier_id', 'version_no', name='uq_templates_courier_version'),
    )
    op.create_table('merchants',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('trading_name', sa.String(200), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, default='active'),
        sa.Column('created_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('anonymised_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('length(name) BETWEEN 1 AND 200', name='ck_merchants_name_len'),
        sa.CheckConstraint("anonymised_at IS NULL OR status = 'closed'", name='ck_merchants_anonymised_only_when_closed'),
        sa.CheckConstraint("status IN ('active','suspended','closing','closed')", name='ck_merchants_status'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=None),
    )
    op.create_table('password_reset_tokens',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=None),
    )
    op.create_index('ix_password_reset_tokens_token_hash', 'password_reset_tokens', ['token_hash'], unique=True)
    op.create_table('user_sessions',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('device_label', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=None),
    )
    op.create_index('ix_user_sessions_token_hash', 'user_sessions', ['token_hash'], unique=True)
    op.create_table('audit_logs',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=True),
        sa.Column('actor_user_id', sa.Uuid(), nullable=True),
        sa.Column('actor_type', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('entity_table', sa.Text(), nullable=True),
        sa.Column('entity_id', sa.Uuid(), nullable=True),
        sa.Column('detail', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_audit_logs_merchant', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name='fk_audit_logs_actor', ondelete='RESTRICT'),
        sa.CheckConstraint("actor_type IN ('user', 'system')", name='ck_audit_logs_actor_type'),
    )
    op.create_index('ix_audit_logs_actor_created', 'audit_logs', ['actor_user_id', 'created_at'], unique=False)
    op.create_index('ix_audit_logs_merchant_action_created', 'audit_logs', ['merchant_id', 'action', 'created_at'], unique=False)
    op.create_index('ix_audit_logs_merchant_created', 'audit_logs', ['merchant_id', 'created_at'], unique=False)
    op.create_table('column_mappings',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('template_id', sa.Uuid(), nullable=False),
        sa.Column('canonical_field', sa.Text(), nullable=False),
        sa.Column('source_header', sa.Text(), nullable=False),
        sa.Column('source_header_normalised', sa.Text(), nullable=False),
        sa.Column('is_required', sa.Boolean(), nullable=False, default=False),
        sa.ForeignKeyConstraint(['template_id'], ['courier_statement_templates.id'], name='fk_column_mappings_template', ondelete='RESTRICT'),
        sa.CheckConstraint("canonical_field IN ('tracking_id', 'merchant_order_ref', 'reported_collected', 'reported_payout', 'delivery_fee', 'cod_fee', 'return_fee', 'other_fee', 'shipment_status', 'statement_date')", name='ck_column_mappings_canonical_field'),
        sa.UniqueConstraint('template_id', 'canonical_field', name='uq_column_mappings_template_field'),
    )
    op.create_index('ix_column_mappings_template', 'column_mappings', ['template_id'], unique=False)
    op.create_table('file_objects',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('storage_key', sa.Text(), nullable=False, unique=True),
        sa.Column('original_filename', sa.Text(), nullable=False),
        sa.Column('sha256', sa.Text(), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('content_type', sa.Text(), nullable=False),
        sa.Column('uploaded_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('merchant_id', 'sha256', name='uq_file_objects_merchant_sha256'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_file_objects_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint('size_bytes >= 0', name='ck_file_objects_size_nonnegative'),
        sa.ForeignKeyConstraint(['uploaded_by_user_id'], ['users.id'], name='fk_file_objects_uploaded_by', ondelete='RESTRICT'),
        sa.CheckConstraint("content_type IN ('text/csv', 'application/vnd.ms-excel')", name='ck_file_objects_content_type'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_file_objects_merchant_id'),
    )
    op.create_table('merchant_courier_accounts',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('courier_id', sa.Uuid(), nullable=False),
        sa.Column('merchant_account_ref', sa.Text(), nullable=True),
        sa.Column('display_label', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['courier_id'], ['couriers.id'], name='fk_mca_courier', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_mca_merchant_id'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_mca_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint("status IN ('active', 'inactive')", name='ck_mca_status'),
    )
    op.create_index('ix_mca_merchant_courier_status', 'merchant_courier_accounts', ['merchant_id', 'courier_id', 'status'], unique=False)
    op.create_table('merchant_memberships',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, default='active'),
        sa.Column('invited_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name=None),
        sa.CheckConstraint("role IN ('owner','admin','member','viewer')", name='ck_membership_role'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=None),
        sa.ForeignKeyConstraint(['invited_by_user_id'], ['users.id'], name=None),
        sa.UniqueConstraint('merchant_id', 'user_id', name='uq_membership_merchant_user'),
        sa.CheckConstraint("status IN ('active','invited','revoked')", name='ck_membership_status'),
    )
    op.create_table('orders',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('merchant_order_ref', sa.Text(), nullable=False),
        sa.Column('order_cod_poisha', sa.BigInteger(), nullable=True),
        sa.Column('currency', sa.Text(), nullable=False, default='BDT'),
        sa.Column('ordered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source', sa.Text(), nullable=False, default='manual'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("currency = 'BDT'", name='ck_orders_currency_bdt'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_orders_merchant_id'),
        sa.CheckConstraint("source IN ('manual', 'csv_import', 'demo_synthetic')", name='ck_orders_source'),
        sa.CheckConstraint('order_cod_poisha IS NULL OR order_cod_poisha >= 0', name='ck_orders_order_cod_nonnegative'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_orders_merchant', ondelete='RESTRICT'),
    )
    op.create_index('ix_orders_merchant_created', 'orders', ['merchant_id', 'created_at'], unique=False)
    op.create_index('ix_orders_merchant_ref', 'orders', ['merchant_id', 'merchant_order_ref'], unique=False)
    op.create_table('workspace_settings',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False, unique=True),
        sa.Column('tolerance_poisha', sa.BigInteger(), nullable=False, default=0),
        sa.Column('locale', sa.Text(), nullable=False, default='en_bn'),
        sa.Column('updated_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_workspace_settings_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint("locale IN ('en', 'en_bn')", name='ck_workspace_settings_locale'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_workspace_settings_merchant_id'),
        sa.CheckConstraint('tolerance_poisha >= 0', name='ck_workspace_settings_tolerance'),
        sa.ForeignKeyConstraint(['updated_by_user_id'], ['users.id'], name='fk_workspace_settings_updated_by', ondelete='RESTRICT'),
    )
    op.create_table('courier_rule_versions',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('merchant_courier_account_id', sa.Uuid(), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, default='draft'),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cod_fee_base', sa.Text(), nullable=True),
        sa.Column('cod_fee_rate_bps', sa.Integer(), nullable=True),
        sa.Column('contractual_delivery_fee_poisha', sa.BigInteger(), nullable=True),
        sa.Column('dc_confirmed', sa.Boolean(), nullable=False, default=False),
        sa.Column('dc_evidence_ref', sa.Text(), nullable=True),
        sa.Column('delivery_fee_poisha', sa.BigInteger(), nullable=True),
        sa.Column('return_fee_poisha', sa.BigInteger(), nullable=True),
        sa.Column('vat_mode', sa.Text(), nullable=True),
        sa.Column('vat_rate_bps', sa.Integer(), nullable=True),
        sa.Column('negative_net_payout_permitted', sa.Boolean(), nullable=False, default=False),
        sa.Column('rule_definition', postgresql.JSONB(), nullable=False),
        sa.Column('content_hash', sa.Text(), nullable=False),
        sa.Column('created_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_rule_versions_merchant_id'),
        sa.CheckConstraint("vat_mode IS NULL OR vat_mode IN ('included_rate_on_net', 'included_rate_on_gross', 'excluded', 'unknown')", name='ck_rule_versions_vat_mode'),
        sa.CheckConstraint("NOT (status = 'active' AND cod_fee_base = 'cod_minus_delivery_fee') OR (dc_confirmed IS TRUE AND dc_evidence_ref IS NOT NULL AND length(dc_evidence_ref) > 0 AND contractual_delivery_fee_poisha IS NOT NULL)", name='ck_rule_versions_dc_active'),
        sa.CheckConstraint('effective_to IS NULL OR effective_to > effective_from', name='ck_rule_versions_effective_window'),
        sa.ForeignKeyConstraint(['merchant_id', 'merchant_courier_account_id'], ['merchant_courier_accounts.merchant_id', 'merchant_courier_accounts.id'], name='fk_rule_versions_account', ondelete='RESTRICT'),
        sa.CheckConstraint('vat_rate_bps IS NULL OR (vat_rate_bps BETWEEN 0 AND 10000)', name='ck_rule_versions_vat_rate_bps'),
        sa.UniqueConstraint('merchant_id', 'merchant_courier_account_id', 'version_no', name='uq_rule_versions_account_version'),
        sa.CheckConstraint('version_no > 0', name='ck_rule_versions_version_no'),
        sa.CheckConstraint("cod_fee_base IS NULL OR cod_fee_base IN ('expected_collectible', 'reported_collected', 'cod_minus_delivery_fee')", name='ck_rule_versions_cod_fee_base'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name='fk_rule_versions_created_by', ondelete='RESTRICT'),
        sa.CheckConstraint("status IN ('draft', 'active', 'superseded', 'retired')", name='ck_rule_versions_status'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_rule_versions_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint('cod_fee_rate_bps IS NULL OR (cod_fee_rate_bps BETWEEN 0 AND 10000)', name='ck_rule_versions_cod_fee_rate_bps'),
    )
    op.create_index('ix_rule_versions_account_status', 'courier_rule_versions', ['merchant_id', 'merchant_courier_account_id', 'status'], unique=False)
    op.create_table('import_batches',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('file_object_id', sa.Uuid(), nullable=False),
        sa.Column('merchant_courier_account_id', sa.Uuid(), nullable=False),
        sa.Column('template_id', sa.Uuid(), nullable=True),
        sa.Column('template_version_no', sa.Integer(), nullable=True),
        sa.Column('mapping_snapshot', postgresql.JSONB(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, default='uploaded'),
        sa.Column('row_count', sa.Integer(), nullable=False, default=0),
        sa.Column('parsed_row_count', sa.Integer(), nullable=False, default=0),
        sa.Column('error_row_count', sa.Integer(), nullable=False, default=0),
        sa.Column('statement_period_start', sa.Date(), nullable=True),
        sa.Column('statement_period_end', sa.Date(), nullable=True),
        sa.Column('created_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('error_row_count >= 0', name='ck_import_batches_error_row_count'),
        sa.CheckConstraint("status IN ('uploaded', 'mapped', 'parsed', 'parse_failed', 'ready', 'superseded')", name='ck_import_batches_status'),
        sa.ForeignKeyConstraint(['merchant_id', 'file_object_id'], ['file_objects.merchant_id', 'file_objects.id'], name='fk_import_batches_file_object', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name='fk_import_batches_created_by', ondelete='RESTRICT'),
        sa.CheckConstraint('row_count >= 0', name='ck_import_batches_row_count'),
        sa.ForeignKeyConstraint(['merchant_id', 'merchant_courier_account_id'], ['merchant_courier_accounts.merchant_id', 'merchant_courier_accounts.id'], name='fk_import_batches_account', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_import_batches_merchant_id'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_import_batches_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint('parsed_row_count >= 0', name='ck_import_batches_parsed_row_count'),
        sa.ForeignKeyConstraint(['template_id'], ['courier_statement_templates.id'], name='fk_import_batches_template', ondelete='RESTRICT'),
    )
    op.create_index('ix_import_batches_merchant_account_created', 'import_batches', ['merchant_id', 'merchant_courier_account_id', 'created_at'], unique=False)
    op.create_index('ix_import_batches_merchant_status_created', 'import_batches', ['merchant_id', 'status', 'created_at'], unique=False)
    op.create_table('shipments',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('order_id', sa.Uuid(), nullable=False),
        sa.Column('merchant_courier_account_id', sa.Uuid(), nullable=True),
        sa.Column('tracking_id_raw', sa.Text(), nullable=True),
        sa.Column('tracking_id_normalised', sa.Text(), nullable=True),
        sa.Column('outcome', sa.Text(), nullable=False, default='unknown'),
        sa.Column('outcome_source', sa.Text(), nullable=True),
        sa.Column('expected_collectible_cod_poisha', sa.BigInteger(), nullable=True),
        sa.Column('partial_amount_evidence_ref', sa.Text(), nullable=True),
        sa.Column('is_cod_bearing', sa.Boolean(), nullable=False, default=False),
        sa.Column('is_superseded', sa.Boolean(), nullable=False, default=False),
        sa.Column('superseded_by_shipment_id', sa.Uuid(), nullable=True),
        sa.Column('superseded_reason', sa.Text(), nullable=True),
        sa.Column('shipped_on', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("outcome IN ('delivered', 'partially_delivered', 'returned', 'cancelled', 'in_transit', 'unknown')", name='ck_shipments_outcome'),
        sa.CheckConstraint('superseded_by_shipment_id IS NULL OR is_superseded IS TRUE', name='ck_shipments_superseded_by_requires_flag'),
        sa.ForeignKeyConstraint(['merchant_id', 'superseded_by_shipment_id'], ['shipments.merchant_id', 'shipments.id'], name='fk_shipments_superseded_by', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id', 'order_id'], ['orders.merchant_id', 'orders.id'], name='fk_shipments_order', ondelete='RESTRICT'),
        sa.CheckConstraint("outcome_source IS NULL OR outcome_source IN ('merchant_confirmed', 'statement', 'system_inferred_label_only')", name='ck_shipments_outcome_source'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_shipments_merchant_id'),
        sa.ForeignKeyConstraint(['merchant_id', 'merchant_courier_account_id'], ['merchant_courier_accounts.merchant_id', 'merchant_courier_accounts.id'], name='fk_shipments_account', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_shipments_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint('expected_collectible_cod_poisha IS NULL OR expected_collectible_cod_poisha >= 0', name='ck_shipments_expected_cod_nonnegative'),
    )
    op.create_index('ix_shipments_merchant_account_outcome', 'shipments', ['merchant_id', 'merchant_courier_account_id', 'outcome'], unique=False)
    op.create_index('ix_shipments_merchant_order', 'shipments', ['merchant_id', 'order_id'], unique=False)
    op.create_index('ix_shipments_merchant_tracking', 'shipments', ['merchant_id', 'tracking_id_normalised'], unique=False)
    op.execute('CREATE UNIQUE INDEX uq_shipments_one_active_cod_bearing_per_order ON shipments (merchant_id, order_id) WHERE is_cod_bearing IS true AND is_superseded IS false')
    op.create_table('courier_rule_evidence',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('rule_version_id', sa.Uuid(), nullable=False),
        sa.Column('evidence_type', sa.Text(), nullable=False),
        sa.Column('evidence_ref', sa.Text(), nullable=False),
        sa.Column('file_object_id', sa.Uuid(), nullable=True),
        sa.Column('confirmed_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('note', sa.Text(), nullable=True),
        sa.CheckConstraint('length(evidence_ref) > 0', name='ck_rule_evidence_ref_nonempty'),
        sa.ForeignKeyConstraint(['merchant_id', 'rule_version_id'], ['courier_rule_versions.merchant_id', 'courier_rule_versions.id'], name='fk_rule_evidence_rule_version', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_rule_evidence_merchant_id'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_rule_evidence_merchant', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id', 'file_object_id'], ['file_objects.merchant_id', 'file_objects.id'], name='fk_rule_evidence_file_object', ondelete='RESTRICT'),
        sa.CheckConstraint("evidence_type IN ('merchant_confirmation', 'signed_rate_card', 'courier_letter', 'statement_annotation', 'other_documented')", name='ck_rule_evidence_type'),
        sa.ForeignKeyConstraint(['confirmed_by_user_id'], ['users.id'], name='fk_rule_evidence_confirmed_by', ondelete='RESTRICT'),
    )
    op.create_index('ix_rule_evidence_rule_version', 'courier_rule_evidence', ['merchant_id', 'rule_version_id'], unique=False)
    op.create_table('import_rows',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('import_batch_id', sa.Uuid(), nullable=False),
        sa.Column('row_number', sa.Integer(), nullable=False),
        sa.Column('raw_values', postgresql.JSONB(), nullable=False),
        sa.Column('raw_line_hash', sa.Text(), nullable=False),
        sa.Column('parse_status', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("parse_status IN ('parsed', 'parsed_with_warnings', 'failed', 'skipped_blank')", name='ck_import_rows_parse_status'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_import_rows_merchant_id'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_import_rows_merchant', ondelete='RESTRICT'),
        sa.UniqueConstraint('import_batch_id', 'row_number', name='uq_import_rows_batch_row_number'),
        sa.CheckConstraint('row_number > 0', name='ck_import_rows_row_number'),
        sa.ForeignKeyConstraint(['merchant_id', 'import_batch_id'], ['import_batches.merchant_id', 'import_batches.id'], name='fk_import_rows_batch', ondelete='RESTRICT'),
    )
    op.create_index('ix_import_rows_batch_row', 'import_rows', ['merchant_id', 'import_batch_id', 'row_number'], unique=False)
    op.create_table('reconciliation_runs',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('import_batch_id', sa.Uuid(), nullable=False),
        sa.Column('merchant_courier_account_id', sa.Uuid(), nullable=False),
        sa.Column('rule_version_id', sa.Uuid(), nullable=True),
        sa.Column('rule_version_no', sa.Integer(), nullable=True),
        sa.Column('rule_content_hash', sa.Text(), nullable=True),
        sa.Column('rule_snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('input_snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('mapping_snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('tolerance_poisha', sa.BigInteger(), nullable=False),
        sa.Column('engine_version', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, default='running'),
        sa.Column('run_hash', sa.Text(), nullable=False),
        sa.Column('created_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_runs_merchant', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'run_hash', name='uq_runs_merchant_run_hash'),
        sa.CheckConstraint('tolerance_poisha >= 0', name='ck_runs_tolerance_nonnegative'),
        sa.ForeignKeyConstraint(['merchant_id', 'import_batch_id'], ['import_batches.merchant_id', 'import_batches.id'], name='fk_runs_import_batch', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name='fk_runs_created_by', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id', 'merchant_courier_account_id'], ['merchant_courier_accounts.merchant_id', 'merchant_courier_accounts.id'], name='fk_runs_account', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_runs_merchant_id'),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name='ck_runs_status'),
        sa.ForeignKeyConstraint(['merchant_id', 'rule_version_id'], ['courier_rule_versions.merchant_id', 'courier_rule_versions.id'], name='fk_runs_rule_version', ondelete='RESTRICT'),
    )
    op.create_index('ix_runs_batch_created', 'reconciliation_runs', ['merchant_id', 'import_batch_id', 'created_at'], unique=False)
    op.create_index('ix_runs_merchant_created', 'reconciliation_runs', ['merchant_id', 'created_at'], unique=False)
    op.create_table('idempotency_keys',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('operation', sa.Text(), nullable=False),
        sa.Column('idempotency_key', sa.Text(), nullable=False),
        sa.Column('request_hash', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, default='in_progress'),
        sa.Column('result_import_batch_id', sa.Uuid(), nullable=True),
        sa.Column('result_reconciliation_run_id', sa.Uuid(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('claim_token', sa.Uuid(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False, default=0),
        sa.Column('claimed_by_instance', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("(status = 'completed' AND completed_at IS NOT NULL AND lease_expires_at IS NULL) OR (status = 'in_progress' AND lease_expires_at IS NOT NULL AND result_import_batch_id IS NULL AND result_reconciliation_run_id IS NULL) OR (status = 'failed' AND lease_expires_at IS NULL AND result_import_batch_id IS NULL AND result_reconciliation_run_id IS NULL)", name='ck_idempotency_keys_state_columns'),
        sa.ForeignKeyConstraint(['merchant_id', 'result_reconciliation_run_id'], ['reconciliation_runs.merchant_id', 'reconciliation_runs.id'], name='fk_idempotency_keys_result_run', ondelete='RESTRICT'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_idempotency_keys_attempt_count'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_idempotency_keys_merchant_id'),
        sa.CheckConstraint("status IN ('in_progress', 'completed', 'failed')", name='ck_idempotency_keys_status'),
        sa.UniqueConstraint('merchant_id', 'operation', 'idempotency_key', name='uq_idempotency_keys_merchant_operation_key'),
        sa.ForeignKeyConstraint(['merchant_id', 'result_import_batch_id'], ['import_batches.merchant_id', 'import_batches.id'], name='fk_idempotency_keys_result_batch', ondelete='RESTRICT'),
        sa.CheckConstraint("(operation = 'import_create' AND result_import_batch_id IS NOT NULL AND result_reconciliation_run_id IS NULL) OR (operation = 'run_create' AND result_reconciliation_run_id IS NOT NULL AND result_import_batch_id IS NULL) OR (status IN ('in_progress', 'failed') AND result_import_batch_id IS NULL AND result_reconciliation_run_id IS NULL)", name='ck_idempotency_keys_typed_reference'),
        sa.CheckConstraint("operation IN ('import_create', 'run_create')", name='ck_idempotency_keys_operation'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_idempotency_keys_merchant', ondelete='RESTRICT'),
    )
    op.create_index('ix_idempotency_keys_merchant_operation_key', 'idempotency_keys', ['merchant_id', 'operation', 'idempotency_key'], unique=False)
    op.create_table('import_errors',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('import_batch_id', sa.Uuid(), nullable=False),
        sa.Column('import_row_id', sa.Uuid(), nullable=True),
        sa.Column('field_name', sa.Text(), nullable=True),
        sa.Column('severity', sa.Text(), nullable=False),
        sa.Column('error_code', sa.Text(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('raw_value', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("severity IN ('error', 'warning')", name='ck_import_errors_severity'),
        sa.ForeignKeyConstraint(['merchant_id', 'import_row_id'], ['import_rows.merchant_id', 'import_rows.id'], name='fk_import_errors_row', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_import_errors_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint("error_code IN ('COLUMN_ABSENT', 'VALUE_BLANK', 'VALUE_INVALID', 'VALUE_UNPARSABLE', 'DUPLICATE_TRACKING', 'AMBIGUOUS_MATCH')", name='ck_import_errors_error_code'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_import_errors_merchant_id'),
        sa.ForeignKeyConstraint(['merchant_id', 'import_batch_id'], ['import_batches.merchant_id', 'import_batches.id'], name='fk_import_errors_batch', ondelete='RESTRICT'),
    )
    op.create_index('ix_import_errors_batch_severity', 'import_errors', ['merchant_id', 'import_batch_id', 'severity'], unique=False)
    op.create_index('ix_import_errors_row', 'import_errors', ['merchant_id', 'import_row_id'], unique=False)
    op.create_table('settlement_batches',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('merchant_courier_account_id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=True),
        sa.Column('period_start', sa.Date(), nullable=True),
        sa.Column('period_end', sa.Date(), nullable=True),
        sa.Column('expected_batch_payout_poisha', sa.BigInteger(), nullable=True),
        sa.Column('allocated_total_poisha', sa.BigInteger(), nullable=True),
        sa.Column('allocation_status', sa.Text(), nullable=False, default='unallocated'),
        sa.Column('bank_status', sa.Text(), nullable=False, default='no_receipt'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("allocation_status IN ('unallocated', 'partially_allocated', 'fully_allocated')", name='ck_settlement_batches_allocation_status'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_settlement_batches_merchant_id'),
        sa.CheckConstraint("bank_status IN ('no_receipt', 'partial_receipt', 'receipt_complete_unallocated', 'fully_reconciled')", name='ck_settlement_batches_bank_status'),
        sa.ForeignKeyConstraint(['merchant_id', 'merchant_courier_account_id'], ['merchant_courier_accounts.merchant_id', 'merchant_courier_accounts.id'], name='fk_settlement_batches_account', ondelete='RESTRICT'),
        sa.CheckConstraint('period_end IS NULL OR period_end >= period_start', name='ck_settlement_batches_period'),
        sa.ForeignKeyConstraint(['merchant_id', 'run_id'], ['reconciliation_runs.merchant_id', 'reconciliation_runs.id'], name='fk_settlement_batches_run', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_settlement_batches_merchant', ondelete='RESTRICT'),
    )
    op.create_index('ix_settlement_batches_account_period', 'settlement_batches', ['merchant_id', 'merchant_courier_account_id', 'period_start'], unique=False)
    op.create_index('ix_settlement_batches_allocation_status', 'settlement_batches', ['merchant_id', 'allocation_status'], unique=False)
    op.create_table('statement_lines',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('import_row_id', sa.Uuid(), nullable=False),
        sa.Column('import_batch_id', sa.Uuid(), nullable=False),
        sa.Column('shipment_id', sa.Uuid(), nullable=True),
        sa.Column('match_status', sa.Text(), nullable=False, default='unmatched'),
        sa.Column('tracking_id_raw', sa.Text(), nullable=True),
        sa.Column('tracking_id_normalised', sa.Text(), nullable=True),
        sa.Column('merchant_order_ref_raw', sa.Text(), nullable=True),
        sa.Column('reported_status_raw', sa.Text(), nullable=True),
        sa.Column('statement_date', sa.Date(), nullable=True),
        sa.Column('reported_collected_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_collected_state', sa.Text(), nullable=False),
        sa.Column('reported_payout_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_payout_state', sa.Text(), nullable=False),
        sa.Column('reported_delivery_fee_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_delivery_fee_state', sa.Text(), nullable=False),
        sa.Column('reported_cod_fee_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_cod_fee_state', sa.Text(), nullable=False),
        sa.Column('reported_return_fee_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_return_fee_state', sa.Text(), nullable=False),
        sa.Column('reported_other_fee_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_other_fee_state', sa.Text(), nullable=False),
        sa.Column('negative_payout_permitted', sa.Boolean(), nullable=False, default=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("reported_delivery_fee_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid')", name='ck_statement_lines_delivery_fee_state'),
        sa.CheckConstraint("reported_collected_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid')", name='ck_statement_lines_collected_state'),
        sa.CheckConstraint("reported_payout_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid')", name='ck_statement_lines_payout_state'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_statement_lines_merchant', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id', 'import_row_id'], ['import_rows.merchant_id', 'import_rows.id'], name='fk_statement_lines_import_row', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id', 'import_batch_id'], ['import_batches.merchant_id', 'import_batches.id'], name='fk_statement_lines_import_batch', ondelete='RESTRICT'),
        sa.CheckConstraint("reported_other_fee_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid')", name='ck_statement_lines_other_fee_state'),
        sa.CheckConstraint("((reported_other_fee_state = 'present_valid' AND reported_other_fee_poisha IS NOT NULL AND reported_other_fee_poisha > 0) OR (reported_other_fee_state = 'explicit_zero' AND reported_other_fee_poisha = 0) OR (reported_other_fee_state IN ('column_absent', 'blank', 'invalid') AND reported_other_fee_poisha IS NULL))", name='ck_statement_lines_other_fee_state_value'),
        sa.ForeignKeyConstraint(['merchant_id', 'shipment_id'], ['shipments.merchant_id', 'shipments.id'], name='fk_statement_lines_shipment', ondelete='RESTRICT'),
        sa.CheckConstraint("match_status IN ('strong_matched', 'human_confirmed', 'unmatched', 'ambiguous', 'duplicate')", name='ck_statement_lines_match_status'),
        sa.UniqueConstraint('merchant_id', 'import_row_id', name='uq_statement_lines_merchant_import_row'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_statement_lines_merchant_id'),
        sa.CheckConstraint("((reported_payout_state = 'present_valid' AND reported_payout_poisha IS NOT NULL AND (reported_payout_poisha >= 0 OR negative_payout_permitted IS TRUE)) OR (reported_payout_state = 'explicit_zero' AND reported_payout_poisha = 0) OR (reported_payout_state IN ('column_absent', 'blank', 'invalid') AND reported_payout_poisha IS NULL))", name='ck_statement_lines_payout_state_value'),
        sa.CheckConstraint("((reported_return_fee_state = 'present_valid' AND reported_return_fee_poisha IS NOT NULL AND reported_return_fee_poisha > 0) OR (reported_return_fee_state = 'explicit_zero' AND reported_return_fee_poisha = 0) OR (reported_return_fee_state IN ('column_absent', 'blank', 'invalid') AND reported_return_fee_poisha IS NULL))", name='ck_statement_lines_return_fee_state_value'),
        sa.CheckConstraint("((reported_collected_state = 'present_valid' AND reported_collected_poisha IS NOT NULL AND reported_collected_poisha > 0) OR (reported_collected_state = 'explicit_zero' AND reported_collected_poisha = 0) OR (reported_collected_state IN ('column_absent', 'blank', 'invalid') AND reported_collected_poisha IS NULL))", name='ck_statement_lines_collected_state_value'),
        sa.CheckConstraint("reported_cod_fee_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid')", name='ck_statement_lines_cod_fee_state'),
        sa.CheckConstraint("((reported_delivery_fee_state = 'present_valid' AND reported_delivery_fee_poisha IS NOT NULL AND reported_delivery_fee_poisha > 0) OR (reported_delivery_fee_state = 'explicit_zero' AND reported_delivery_fee_poisha = 0) OR (reported_delivery_fee_state IN ('column_absent', 'blank', 'invalid') AND reported_delivery_fee_poisha IS NULL))", name='ck_statement_lines_delivery_fee_state_value'),
        sa.CheckConstraint("((reported_cod_fee_state = 'present_valid' AND reported_cod_fee_poisha IS NOT NULL AND reported_cod_fee_poisha > 0) OR (reported_cod_fee_state = 'explicit_zero' AND reported_cod_fee_poisha = 0) OR (reported_cod_fee_state IN ('column_absent', 'blank', 'invalid') AND reported_cod_fee_poisha IS NULL))", name='ck_statement_lines_cod_fee_state_value'),
        sa.CheckConstraint("reported_return_fee_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid')", name='ck_statement_lines_return_fee_state'),
    )
    op.create_index('ix_statement_lines_batch', 'statement_lines', ['merchant_id', 'import_batch_id'], unique=False)
    op.create_index('ix_statement_lines_shipment', 'statement_lines', ['merchant_id', 'shipment_id'], unique=False)
    op.create_index('ix_statement_lines_tracking', 'statement_lines', ['merchant_id', 'tracking_id_normalised'], unique=False)
    op.execute("CREATE INDEX ix_statement_lines_unmatched ON statement_lines (merchant_id, match_status) WHERE match_status IN ('unmatched', 'ambiguous')")
    op.create_table('bank_receipts',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('settlement_batch_id', sa.Uuid(), nullable=True),
        sa.Column('amount_poisha', sa.BigInteger(), nullable=False),
        sa.Column('received_on', sa.Date(), nullable=True),
        sa.Column('receipt_ref_label', sa.Text(), nullable=True),
        sa.Column('is_complete', sa.Boolean(), nullable=False, default=False),
        sa.Column('marked_complete_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('marked_complete_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('evidence_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['merchant_id', 'settlement_batch_id'], ['settlement_batches.merchant_id', 'settlement_batches.id'], name='fk_bank_receipts_batch', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'settlement_batch_id', 'id', name='uq_bank_receipts_merchant_batch_id'),
        sa.CheckConstraint('amount_poisha > 0', name='ck_bank_receipts_amount_positive'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_bank_receipts_merchant', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['marked_complete_by_user_id'], ['users.id'], name='fk_bank_receipts_marked_complete_by', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_bank_receipts_merchant_id'),
        sa.CheckConstraint('is_complete IS FALSE OR (marked_complete_by_user_id IS NOT NULL AND marked_complete_at IS NOT NULL)', name='ck_bank_receipts_complete_requires_actor'),
    )
    op.create_index('ix_bank_receipts_batch', 'bank_receipts', ['merchant_id', 'settlement_batch_id'], unique=False)
    op.create_index('ix_bank_receipts_received_on', 'bank_receipts', ['merchant_id', 'received_on'], unique=False)
    op.create_table('reconciliation_results',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=False),
        sa.Column('shipment_id', sa.Uuid(), nullable=False),
        sa.Column('statement_line_id', sa.Uuid(), nullable=True),
        sa.Column('expected_collectible_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_collected_poisha', sa.BigInteger(), nullable=True),
        sa.Column('c_poisha', sa.BigInteger(), nullable=True),
        sa.Column('x_e_poisha', sa.BigInteger(), nullable=True),
        sa.Column('x_r_poisha', sa.BigInteger(), nullable=True),
        sa.Column('x_upper_r_poisha', sa.BigInteger(), nullable=True),
        sa.Column('delta_poisha', sa.BigInteger(), nullable=True),
        sa.Column('v_r_poisha', sa.BigInteger(), nullable=True),
        sa.Column('p_e_poisha', sa.BigInteger(), nullable=True),
        sa.Column('p_cond_poisha', sa.BigInteger(), nullable=True),
        sa.Column('p_r_poisha', sa.BigInteger(), nullable=True),
        sa.Column('s_poisha', sa.BigInteger(), nullable=True),
        sa.Column('v_cond_poisha', sa.BigInteger(), nullable=True),
        sa.Column('v_total_poisha', sa.BigInteger(), nullable=True),
        sa.Column('contractual_payout_status', sa.Text(), nullable=False),
        sa.Column('data_quality_status', sa.Text(), nullable=False),
        sa.Column('overall_status', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['merchant_id', 'shipment_id'], ['shipments.merchant_id', 'shipments.id'], name='fk_results_shipment', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_results_merchant', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'run_id', 'shipment_id', name='uq_results_merchant_run_shipment'),
        sa.CheckConstraint("contractual_payout_status IN ('matched', 'mismatch', 'matched_with_tolerance', 'not_calculable')", name='ck_results_contractual_payout_status'),
        sa.CheckConstraint('((s_poisha IS NOT NULL AND v_r_poisha IS NOT NULL AND v_cond_poisha = (s_poisha - v_r_poisha)) OR (s_poisha IS NULL OR v_r_poisha IS NULL AND v_cond_poisha IS NULL))', name='ck_results_v_cond_identity'),
        sa.ForeignKeyConstraint(['merchant_id', 'statement_line_id'], ['statement_lines.merchant_id', 'statement_lines.id'], name='fk_results_statement_line', ondelete='RESTRICT'),
        sa.CheckConstraint('((c_poisha IS NOT NULL AND delta_poisha IS NOT NULL AND v_r_poisha IS NOT NULL AND s_poisha IS NOT NULL AND v_total_poisha = (c_poisha - delta_poisha - v_r_poisha + s_poisha)) OR (c_poisha IS NULL OR delta_poisha IS NULL OR v_r_poisha IS NULL OR s_poisha IS NULL AND v_total_poisha IS NULL))', name='ck_results_v_total_identity'),
        sa.CheckConstraint("data_quality_status IN ('ok', 'has_warnings', 'needs_review')", name='ck_results_data_quality_status'),
        sa.ForeignKeyConstraint(['merchant_id', 'run_id'], ['reconciliation_runs.merchant_id', 'reconciliation_runs.id'], name='fk_results_run', ondelete='RESTRICT'),
        sa.CheckConstraint("overall_status IN ('matched', 'open', 'mismatch', 'needs_review', 'matched_with_tolerance')", name='ck_results_overall_status'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_results_merchant_id'),
    )
    op.create_index('ix_results_run', 'reconciliation_results', ['merchant_id', 'run_id'], unique=False)
    op.create_index('ix_results_shipment', 'reconciliation_results', ['merchant_id', 'shipment_id'], unique=False)
    op.execute("CREATE INDEX ix_results_work_queue ON reconciliation_results (merchant_id, overall_status) WHERE overall_status IN ('open', 'mismatch', 'needs_review')")
    op.create_table('settlement_variance_calculations',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('settlement_batch_id', sa.Uuid(), nullable=False),
        sa.Column('calculation_seq', sa.Integer(), nullable=False),
        sa.Column('expected_batch_payout_poisha', sa.BigInteger(), nullable=False),
        sa.Column('allocated_bank_confirmed_total_poisha', sa.BigInteger(), nullable=False),
        sa.Column('bank_variance_poisha', sa.BigInteger(), nullable=False),
        sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('computed_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('actor_type', sa.Text(), nullable=False, default='system'),
        sa.UniqueConstraint('merchant_id', 'settlement_batch_id', 'id', name='uq_variance_calcs_merchant_batch_id'),
        sa.CheckConstraint('calculation_seq >= 1', name='ck_variance_calcs_seq'),
        sa.ForeignKeyConstraint(['computed_by_user_id'], ['users.id'], name='fk_variance_calcs_computed_by', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_variance_calcs_merchant', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_variance_calcs_merchant_id'),
        sa.CheckConstraint('bank_variance_poisha = allocated_bank_confirmed_total_poisha - expected_batch_payout_poisha', name='ck_variance_calcs_arithmetic'),
        sa.ForeignKeyConstraint(['merchant_id', 'settlement_batch_id'], ['settlement_batches.merchant_id', 'settlement_batches.id'], name='fk_variance_calcs_batch', ondelete='RESTRICT'),
        sa.CheckConstraint("actor_type IN ('user', 'system')", name='ck_variance_calcs_actor_type'),
        sa.UniqueConstraint('merchant_id', 'settlement_batch_id', 'calculation_seq', name='uq_variance_calcs_merchant_batch_seq'),
    )
    op.create_index('ix_variance_calcs_batch_seq', 'settlement_variance_calculations', ['merchant_id', 'settlement_batch_id', 'calculation_seq'], unique=False)
    op.create_table('shipment_match_candidates',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('statement_line_id', sa.Uuid(), nullable=False),
        sa.Column('shipment_id', sa.Uuid(), nullable=False),
        sa.Column('match_basis', sa.Text(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('prior_candidate_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('rank > 0', name='ck_match_candidates_rank'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_match_candidates_merchant_id'),
        sa.ForeignKeyConstraint(['merchant_id', 'shipment_id'], ['shipments.merchant_id', 'shipments.id'], name='fk_match_candidates_shipment', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_match_candidates_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint("match_basis IN ('strong_tracking', 'weak_order_ref')", name='ck_match_candidates_basis'),
        sa.ForeignKeyConstraint(['merchant_id', 'prior_candidate_id'], ['shipment_match_candidates.merchant_id', 'shipment_match_candidates.id'], name='fk_match_candidates_prior_candidate', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id', 'statement_line_id'], ['statement_lines.merchant_id', 'statement_lines.id'], name='fk_match_candidates_statement_line', ondelete='RESTRICT'),
    )
    op.create_index('ix_match_candidates_line', 'shipment_match_candidates', ['merchant_id', 'statement_line_id'], unique=False)
    op.create_index('ix_match_candidates_shipment', 'shipment_match_candidates', ['merchant_id', 'shipment_id'], unique=False)
    op.create_table('reconciliation_fee_lines',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('result_id', sa.Uuid(), nullable=False),
        sa.Column('fee_code', sa.Text(), nullable=False),
        sa.Column('component_seq', sa.Integer(), nullable=False, default=1),
        sa.Column('source_statement_field', sa.Text(), nullable=True),
        sa.Column('expected_basis_poisha', sa.BigInteger(), nullable=True),
        sa.Column('expected_basis_state', sa.Text(), nullable=False),
        sa.Column('reported_basis_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_basis_state', sa.Text(), nullable=False),
        sa.Column('reported_actual_poisha', sa.BigInteger(), nullable=True),
        sa.Column('reported_actual_state', sa.Text(), nullable=False),
        sa.Column('included_tax_poisha', sa.BigInteger(), nullable=True),
        sa.Column('rounding_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('merchant_id', 'result_id', 'fee_code', 'component_seq', name='uq_fee_lines_merchant_result_fee_seq'),
        sa.CheckConstraint("((reported_basis_state = 'present_valid' AND reported_basis_poisha IS NOT NULL) OR (reported_basis_state = 'explicit_zero' AND reported_basis_poisha = 0) OR (reported_basis_state IN ('column_absent', 'blank', 'invalid', 'rule_unknown', 'contract_missing') AND reported_basis_poisha IS NULL))", name='ck_fee_lines_reported_basis_state_value'),
        sa.CheckConstraint("expected_basis_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid', 'rule_unknown', 'contract_missing')", name='ck_fee_lines_expected_basis_state'),
        sa.ForeignKeyConstraint(['merchant_id', 'result_id'], ['reconciliation_results.merchant_id', 'reconciliation_results.id'], name='fk_fee_lines_result', ondelete='RESTRICT'),
        sa.CheckConstraint("fee_code IN ('delivery_fee', 'cod_fee', 'return_fee', 'vat_on_fee', 'other_fee')", name='ck_fee_lines_fee_code'),
        sa.CheckConstraint("((reported_actual_state = 'present_valid' AND reported_actual_poisha IS NOT NULL) OR (reported_actual_state = 'explicit_zero' AND reported_actual_poisha = 0) OR (reported_actual_state IN ('column_absent', 'blank', 'invalid', 'rule_unknown', 'contract_missing') AND reported_actual_poisha IS NULL))", name='ck_fee_lines_reported_actual_state_value'),
        sa.CheckConstraint("reported_basis_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid', 'rule_unknown', 'contract_missing')", name='ck_fee_lines_reported_basis_state'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_fee_lines_merchant_id'),
        sa.CheckConstraint('component_seq >= 1', name='ck_fee_lines_component_seq'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_fee_lines_merchant', ondelete='RESTRICT'),
        sa.CheckConstraint("reported_actual_state IN ('present_valid', 'explicit_zero', 'column_absent', 'blank', 'invalid', 'rule_unknown', 'contract_missing')", name='ck_fee_lines_reported_actual_state'),
        sa.CheckConstraint("((expected_basis_state = 'present_valid' AND expected_basis_poisha IS NOT NULL) OR (expected_basis_state = 'explicit_zero' AND expected_basis_poisha = 0) OR (expected_basis_state IN ('column_absent', 'blank', 'invalid', 'rule_unknown', 'contract_missing') AND expected_basis_poisha IS NULL))", name='ck_fee_lines_expected_basis_state_value'),
    )
    op.create_index('ix_fee_lines_result', 'reconciliation_fee_lines', ['merchant_id', 'result_id'], unique=False)
    op.create_table('reconciliation_findings',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=False),
        sa.Column('result_id', sa.Uuid(), nullable=False),
        sa.Column('category', sa.Text(), nullable=False),
        sa.Column('finding_type', sa.Text(), nullable=False),
        sa.Column('finding_code', sa.Text(), nullable=False),
        sa.Column('amount_poisha', sa.BigInteger(), nullable=True),
        sa.Column('is_within_tolerance', sa.Boolean(), nullable=False, default=False),
        sa.Column('explanation_en', sa.Text(), nullable=False),
        sa.Column('explanation_bn', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_findings_merchant_id'),
        sa.CheckConstraint("finding_type IN ('collection', 'fee_tax', 'statement_arithmetic', 'conditional_payout', 'scenario', 'data_quality')", name='ck_findings_finding_type'),
        sa.ForeignKeyConstraint(['merchant_id', 'run_id'], ['reconciliation_runs.merchant_id', 'reconciliation_runs.id'], name='fk_findings_run', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'result_id', 'finding_code', name='uq_findings_merchant_result_code'),
        sa.CheckConstraint("category IN ('financial', 'data_quality')", name='ck_findings_category'),
        sa.ForeignKeyConstraint(['merchant_id', 'result_id'], ['reconciliation_results.merchant_id', 'reconciliation_results.id'], name='fk_findings_result', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_findings_merchant', ondelete='RESTRICT'),
    )
    op.create_index('ix_findings_code', 'reconciliation_findings', ['merchant_id', 'finding_code'], unique=False)
    op.execute("CREATE INDEX ix_findings_dq_queue ON reconciliation_findings (merchant_id, category) WHERE category = 'data_quality'")
    op.create_index('ix_findings_run_category', 'reconciliation_findings', ['merchant_id', 'run_id', 'category'], unique=False)
    op.create_table('settlement_allocations',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('settlement_batch_id', sa.Uuid(), nullable=False),
        sa.Column('bank_receipt_id', sa.Uuid(), nullable=False),
        sa.Column('allocated_poisha', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False, default='allocation'),
        sa.Column('reverses_allocation_id', sa.Uuid(), nullable=True),
        sa.Column('created_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("(kind = 'reversal' AND reverses_allocation_id IS NOT NULL) OR (kind = 'allocation' AND reverses_allocation_id IS NULL)", name='ck_allocations_reversal_reference'),
        sa.UniqueConstraint('merchant_id', 'settlement_batch_id', 'id', name='uq_allocations_merchant_batch_id'),
        sa.CheckConstraint('allocated_poisha > 0', name='ck_allocations_amount_positive'),
        sa.ForeignKeyConstraint(['merchant_id', 'settlement_batch_id'], ['settlement_batches.merchant_id', 'settlement_batches.id'], name='fk_allocations_batch', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name='fk_allocations_created_by', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_allocations_merchant', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id', 'bank_receipt_id'], ['bank_receipts.merchant_id', 'bank_receipts.id'], name='fk_allocations_receipt', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_allocations_merchant_id'),
        sa.CheckConstraint("kind IN ('allocation', 'reversal')", name='ck_allocations_kind'),
        sa.ForeignKeyConstraint(['merchant_id', 'reverses_allocation_id'], ['settlement_allocations.merchant_id', 'settlement_allocations.id'], name='fk_allocations_reverses', ondelete='RESTRICT'),
    )
    op.create_index('ix_allocations_batch', 'settlement_allocations', ['merchant_id', 'settlement_batch_id'], unique=False)
    op.create_index('ix_allocations_receipt', 'settlement_allocations', ['merchant_id', 'bank_receipt_id'], unique=False)
    op.execute("CREATE UNIQUE INDEX uq_allocations_one_reversal ON settlement_allocations (reverses_allocation_id) WHERE kind = 'reversal'")
    op.create_table('shipment_match_decisions',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('candidate_id', sa.Uuid(), nullable=False),
        sa.Column('decision', sa.Text(), nullable=False),
        sa.Column('decided_by_user_id', sa.Uuid(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason_note', sa.Text(), nullable=True),
        sa.Column('supersedes_decision_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['merchant_id', 'candidate_id'], ['shipment_match_candidates.merchant_id', 'shipment_match_candidates.id'], name='fk_match_decisions_candidate', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_match_decisions_merchant_id'),
        sa.CheckConstraint('supersedes_decision_id IS NULL OR supersedes_decision_id <> id', name='ck_match_decisions_no_self_supersede'),
        sa.ForeignKeyConstraint(['decided_by_user_id'], ['users.id'], name='fk_match_decisions_decided_by', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_match_decisions_merchant', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'candidate_id', 'id', name='uq_match_decisions_merchant_candidate_id'),
        sa.CheckConstraint("decision IN ('confirmed', 'rejected', 'superseded')", name='ck_match_decisions_decision'),
        sa.ForeignKeyConstraint(['merchant_id', 'candidate_id', 'supersedes_decision_id'], ['shipment_match_decisions.merchant_id', 'shipment_match_decisions.candidate_id', 'shipment_match_decisions.id'], name='fk_match_decisions_supersedes', ondelete='RESTRICT'),
    )
    op.create_index('ix_match_decisions_candidate', 'shipment_match_decisions', ['merchant_id', 'candidate_id'], unique=False)
    op.execute('CREATE UNIQUE INDEX uq_match_decisions_one_successor ON shipment_match_decisions (merchant_id, supersedes_decision_id) WHERE supersedes_decision_id IS NOT NULL')
    op.create_table('settlement_variance_calculation_inputs',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False, default=uuid.uuid4),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column('settlement_batch_id', sa.Uuid(), nullable=False),
        sa.Column('calculation_id', sa.Uuid(), nullable=False),
        sa.Column('input_kind', sa.Text(), nullable=False),
        sa.Column('bank_receipt_id', sa.Uuid(), nullable=True),
        sa.Column('settlement_allocation_id', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name='fk_variance_inputs_merchant', ondelete='RESTRICT'),
        sa.UniqueConstraint('merchant_id', 'id', name='uq_variance_inputs_merchant_id'),
        sa.CheckConstraint("(input_kind = 'receipt' AND bank_receipt_id IS NOT NULL AND settlement_allocation_id IS NULL) OR (input_kind = 'allocation' AND settlement_allocation_id IS NOT NULL AND bank_receipt_id IS NULL)", name='ck_variance_inputs_exactly_one_reference'),
        sa.ForeignKeyConstraint(['merchant_id', 'settlement_batch_id', 'bank_receipt_id'], ['bank_receipts.merchant_id', 'bank_receipts.settlement_batch_id', 'bank_receipts.id'], name='fk_variance_inputs_receipt', ondelete='RESTRICT'),
        sa.CheckConstraint("input_kind IN ('receipt', 'allocation')", name='ck_variance_inputs_kind'),
        sa.ForeignKeyConstraint(['merchant_id', 'settlement_batch_id', 'settlement_allocation_id'], ['settlement_allocations.merchant_id', 'settlement_allocations.settlement_batch_id', 'settlement_allocations.id'], name='fk_variance_inputs_allocation', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['merchant_id', 'settlement_batch_id', 'calculation_id'], ['settlement_variance_calculations.merchant_id', 'settlement_variance_calculations.settlement_batch_id', 'settlement_variance_calculations.id'], name='fk_variance_inputs_calculation', ondelete='RESTRICT'),
    )
    op.create_index('ix_variance_inputs_calculation', 'settlement_variance_calculation_inputs', ['merchant_id', 'calculation_id'], unique=False)
    # Row-Level Security: ENABLE + FORCE on the tenant root and every
    # tenant-owned table. Policies are defined in app/db/rls.sql.
    op.execute("ALTER TABLE merchants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE merchants FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE merchant_memberships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE merchant_memberships FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE merchant_courier_accounts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE merchant_courier_accounts FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE courier_rule_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE courier_rule_versions FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE courier_rule_evidence ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE courier_rule_evidence FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE orders ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE orders FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE shipments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE shipments FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_objects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_objects FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE import_batches FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE import_rows ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE import_rows FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE import_errors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE import_errors FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE statement_lines ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE statement_lines FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE shipment_match_candidates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE shipment_match_candidates FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE shipment_match_decisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE shipment_match_decisions FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliation_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliation_runs FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliation_results ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliation_results FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliation_fee_lines ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliation_fee_lines FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliation_findings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE reconciliation_findings FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE settlement_batches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE settlement_batches FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bank_receipts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bank_receipts FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE settlement_allocations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE settlement_allocations FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE settlement_variance_calculations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE settlement_variance_calculations FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE settlement_variance_calculation_inputs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE settlement_variance_calculation_inputs FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspace_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspace_settings FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE idempotency_keys FORCE ROW LEVEL SECURITY")


def downgrade():
    op.drop_table('settlement_variance_calculation_inputs')
    op.drop_table('shipment_match_decisions')
    op.drop_table('settlement_allocations')
    op.drop_table('reconciliation_findings')
    op.drop_table('reconciliation_fee_lines')
    op.drop_table('shipment_match_candidates')
    op.drop_table('settlement_variance_calculations')
    op.drop_table('reconciliation_results')
    op.drop_table('bank_receipts')
    op.drop_table('statement_lines')
    op.drop_table('settlement_batches')
    op.drop_table('import_errors')
    op.drop_table('idempotency_keys')
    op.drop_table('reconciliation_runs')
    op.drop_table('import_rows')
    op.drop_table('courier_rule_evidence')
    op.drop_table('shipments')
    op.drop_table('import_batches')
    op.drop_table('courier_rule_versions')
    op.drop_table('workspace_settings')
    op.drop_table('orders')
    op.drop_table('merchant_memberships')
    op.drop_table('merchant_courier_accounts')
    op.drop_table('file_objects')
    op.drop_table('column_mappings')
    op.drop_table('audit_logs')
    op.drop_table('user_sessions')
    op.drop_table('password_reset_tokens')
    op.drop_table('merchants')
    op.drop_table('courier_statement_templates')
    op.drop_table('users')
    op.drop_table('couriers')
