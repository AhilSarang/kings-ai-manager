"""email agent

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17 09:38:24.973190
"""
import json
from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.seed_data import EMAIL_ALERT_RULES, EMAIL_SETTINGS
import sqlalchemy as sa

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for seq in ("email_thread_ref_seq", "email_draft_ref_seq"):
        op.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1")
    op.create_table('email_accounts',
    sa.Column('address', sa.String(length=320), nullable=False),
    sa.Column('provider', sa.String(length=20), server_default=sa.text("'gmail'"), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'paused'"), nullable=False),
    sa.Column('encrypted_refresh_token', sa.Text(), nullable=True),
    sa.Column('last_history_id', sa.BigInteger(), nullable=True),
    sa.Column('watch_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('backfill_completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("provider = 'gmail'", name=op.f('ck_email_accounts_provider')),
    sa.CheckConstraint("status IN ('active', 'auth_error', 'paused')", name=op.f('ck_email_accounts_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_accounts')),
    sa.UniqueConstraint('address', name=op.f('uq_email_accounts_address'))
    )
    op.create_table('email_alert_rules',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('rule_type', sa.String(length=30), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('alert_level', sa.String(length=30), nullable=False),
    sa.Column('bypass_quiet_hours', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by', sa.String(length=10), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("alert_level IN ('instant', 'daily_summary_only', 'ignore')", name=op.f('ck_email_alert_rules_alert_level')),
    sa.CheckConstraint("created_by IN ('system', 'owner')", name=op.f('ck_email_alert_rules_created_by')),
    sa.CheckConstraint("rule_type IN ('builtin_category', 'sender', 'domain', 'keyword', 'custom_description')", name=op.f('ck_email_alert_rules_rule_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_alert_rules'))
    )
    op.create_table('email_contacts',
    sa.Column('address', sa.String(length=320), nullable=False),
    sa.Column('name', sa.String(length=320), nullable=True),
    sa.Column('domain', sa.String(length=255), nullable=False),
    sa.Column('type', sa.String(length=20), server_default=sa.text("'other'"), nullable=False),
    sa.Column('type_set_by_owner', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_vip', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("type IN ('amazon', 'supplier', 'customer_b2b', 'customer_b2c', 'logistics', 'legal', 'finance', 'other')", name=op.f('ck_email_contacts_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_contacts')),
    sa.UniqueConstraint('address', name=op.f('uq_email_contacts_address'))
    )
    op.create_index(op.f('ix_email_contacts_domain'), 'email_contacts', ['domain'], unique=False)
    op.create_table('email_threads',
    sa.Column('ref', sa.String(length=20), server_default=sa.text("'E-' || nextval('email_thread_ref_seq')"), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('provider_thread_id', sa.String(length=64), nullable=False),
    sa.Column('subject', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('participants', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('first_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_message_from_owner', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('category', sa.String(length=30), nullable=True),
    sa.Column('importance', sa.String(length=10), nullable=True),
    sa.Column('needs_reply', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'open'"), nullable=False),
    sa.Column('follow_up_due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('follow_up_tracking', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('alert_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('alerted_rules', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('alerted_importance', sa.String(length=10), nullable=True),
    sa.Column('linked_task_id', sa.UUID(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('suggested_action', sa.Text(), nullable=True),
    sa.Column('risk_flags', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('triage_version', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('amazon_case_id', sa.String(length=64), nullable=True),
    sa.Column('supplier_id', sa.String(length=64), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("category IS NULL OR category IN ('amazon', 'supplier', 'customer_b2b', 'customer_b2c', 'logistics', 'finance_invoice', 'legal', 'marketing_spam', 'personal', 'other')", name=op.f('ck_email_threads_category')),
    sa.CheckConstraint("importance IS NULL OR importance IN ('critical', 'high', 'normal', 'low')", name=op.f('ck_email_threads_importance')),
    sa.CheckConstraint("status IN ('open', 'waiting_on_them', 'handled', 'ignored')", name=op.f('ck_email_threads_status')),
    sa.ForeignKeyConstraint(['account_id'], ['email_accounts.id'], name=op.f('fk_email_threads_account_id_email_accounts')),
    sa.ForeignKeyConstraint(['linked_task_id'], ['tasks.id'], name=op.f('fk_email_threads_linked_task_id_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_threads')),
    sa.UniqueConstraint('account_id', 'provider_thread_id', name='uq_email_threads_account_thread'),
    sa.UniqueConstraint('ref', name=op.f('uq_email_threads_ref'))
    )
    op.create_index(op.f('ix_email_threads_follow_up_due_at'), 'email_threads', ['follow_up_due_at'], unique=False)
    op.create_index('ix_email_threads_status_last', 'email_threads', ['status', 'last_message_at'], unique=False)
    op.create_table('email_drafts',
    sa.Column('ref', sa.String(length=20), server_default=sa.text("'D-' || nextval('email_draft_ref_seq')"), nullable=False),
    sa.Column('thread_id', sa.UUID(), nullable=False),
    sa.Column('requested_by_user_id', sa.UUID(), nullable=True),
    sa.Column('owner_instructions', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('to', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('cc', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('subject', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('body_text', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('gmail_draft_id', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('approval_id', sa.UUID(), nullable=True),
    sa.Column('sent_provider_message_id', sa.String(length=64), nullable=True),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'pending_approval', 'sent', 'discarded', 'send_failed')", name=op.f('ck_email_drafts_status')),
    sa.ForeignKeyConstraint(['approval_id'], ['approvals.id'], name=op.f('fk_email_drafts_approval_id_approvals')),
    sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id'], name=op.f('fk_email_drafts_requested_by_user_id_users')),
    sa.ForeignKeyConstraint(['thread_id'], ['email_threads.id'], name=op.f('fk_email_drafts_thread_id_email_threads')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_drafts')),
    sa.UniqueConstraint('ref', name=op.f('uq_email_drafts_ref'))
    )
    op.create_index(op.f('ix_email_drafts_thread_id'), 'email_drafts', ['thread_id'], unique=False)
    op.create_table('email_messages',
    sa.Column('thread_id', sa.UUID(), nullable=False),
    sa.Column('provider_message_id', sa.String(length=64), nullable=False),
    sa.Column('rfc_message_id', sa.String(length=998), nullable=True),
    sa.Column('from_address', sa.String(length=320), server_default=sa.text("''"), nullable=False),
    sa.Column('from_name', sa.String(length=320), nullable=True),
    sa.Column('to', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('cc', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('reply_to', sa.String(length=998), nullable=True),
    sa.Column('subject', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('body_text', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('snippet', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('gmail_labels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('gmail_category', sa.String(length=20), nullable=True),
    sa.Column('auth_spf', sa.String(length=10), server_default=sa.text("'none'"), nullable=False),
    sa.Column('auth_dkim', sa.String(length=10), server_default=sa.text("'none'"), nullable=False),
    sa.Column('auth_dmarc', sa.String(length=10), server_default=sa.text("'none'"), nullable=False),
    sa.Column('dkim_domains', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('is_from_owner', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_self_sent', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('possible_phishing', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('headers', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('triage_status', sa.String(length=10), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('triage_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('triaged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("auth_dkim IN ('pass', 'fail', 'none')", name=op.f('ck_email_messages_auth_dkim')),
    sa.CheckConstraint("auth_dmarc IN ('pass', 'fail', 'none')", name=op.f('ck_email_messages_auth_dmarc')),
    sa.CheckConstraint("auth_spf IN ('pass', 'fail', 'none')", name=op.f('ck_email_messages_auth_spf')),
    sa.CheckConstraint("triage_status IN ('pending', 'done', 'failed', 'skipped', 'backfill')", name=op.f('ck_email_messages_triage_status')),
    sa.ForeignKeyConstraint(['thread_id'], ['email_threads.id'], name=op.f('fk_email_messages_thread_id_email_threads')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_messages')),
    sa.UniqueConstraint('provider_message_id', name=op.f('uq_email_messages_provider_message_id'))
    )
    op.create_index(op.f('ix_email_messages_from_address'), 'email_messages', ['from_address'], unique=False)
    op.create_index('ix_email_messages_thread_sent', 'email_messages', ['thread_id', 'sent_at'], unique=False)
    op.create_index('ix_email_messages_triage', 'email_messages', ['triage_status', 'created_at'], unique=False)
    op.create_table('email_attachments',
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=True),
    sa.Column('provider_attachment_id', sa.Text(), nullable=True),
    sa.Column('filename', sa.String(length=255), server_default=sa.text("''"), nullable=False),
    sa.Column('mime_type', sa.String(length=255), server_default=sa.text("''"), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('skipped_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], name=op.f('fk_email_attachments_file_id_files')),
    sa.ForeignKeyConstraint(['message_id'], ['email_messages.id'], name=op.f('fk_email_attachments_message_id_email_messages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_attachments'))
    )
    op.create_index(op.f('ix_email_attachments_message_id'), 'email_attachments', ['message_id'], unique=False)

    # files.source gains 'email'
    op.drop_constraint("source", "files", type_="check")
    op.create_check_constraint("source", "files", "source IN ('whatsapp', 'agent', 'upload', 'email')")

    rules = sa.table(
        "email_alert_rules",
        sa.column("name", sa.String), sa.column("rule_type", sa.String), sa.column("value", sa.Text),
        sa.column("alert_level", sa.String), sa.column("bypass_quiet_hours", sa.Boolean),
        sa.column("created_by", sa.String), sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(rules, EMAIL_ALERT_RULES)
    for key, value in EMAIL_SETTINGS.items():
        op.execute(
            sa.text("INSERT INTO settings (key, value) VALUES (:k, CAST(:v AS jsonb)) ON CONFLICT (key) DO NOTHING")
            .bindparams(k=key, v=json.dumps(value))
        )

def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key IN ('email_followup_business_days', "
               "'email_large_order_threshold_usd', 'email_triage_batch_size', 'email_paused')")
    op.execute("DELETE FROM files WHERE source = 'email'")
    op.drop_constraint("source", "files", type_="check")
    op.create_check_constraint("source", "files", "source IN ('whatsapp', 'agent', 'upload')")
    op.drop_index(op.f('ix_email_attachments_message_id'), table_name='email_attachments')
    op.drop_table('email_attachments')
    op.drop_index('ix_email_messages_triage', table_name='email_messages')
    op.drop_index('ix_email_messages_thread_sent', table_name='email_messages')
    op.drop_index(op.f('ix_email_messages_from_address'), table_name='email_messages')
    op.drop_table('email_messages')
    op.drop_index(op.f('ix_email_drafts_thread_id'), table_name='email_drafts')
    op.drop_table('email_drafts')
    op.drop_index('ix_email_threads_status_last', table_name='email_threads')
    op.drop_index(op.f('ix_email_threads_follow_up_due_at'), table_name='email_threads')
    op.drop_table('email_threads')
    op.drop_index(op.f('ix_email_contacts_domain'), table_name='email_contacts')
    op.drop_table('email_contacts')
    op.drop_table('email_alert_rules')
    op.drop_table('email_accounts')
    for seq in ("email_thread_ref_seq", "email_draft_ref_seq"):
        op.execute(f"DROP SEQUENCE IF EXISTS {seq}")
