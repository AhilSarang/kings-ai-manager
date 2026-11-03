"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-09-16 11:10:16.433746
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import postgresql  # noqa: F401

from app.db.seed_data import AGENTS, DEFAULT_SETTINGS
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for seq in ("task_ref_seq", "approval_ref_seq", "error_ref_seq"):
        op.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1")
    op.create_table('agents',
    sa.Column('key', sa.String(length=40), nullable=False),
    sa.Column('display_name', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('permissions', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'inactive', 'paused')", name=op.f('ck_agents_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agents')),
    sa.UniqueConstraint('key', name=op.f('uq_agents_key'))
    )
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor_type', sa.String(length=10), nullable=False),
    sa.Column('actor_id', sa.String(length=64), nullable=True),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('target_type', sa.String(length=40), nullable=True),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('trigger_message_id', sa.UUID(), nullable=True),
    sa.Column('llm_call_id', sa.UUID(), nullable=True),
    sa.CheckConstraint("actor_type IN ('user', 'agent', 'system')", name=op.f('ck_audit_log_actor_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'], unique=False)
    op.create_index('ix_audit_log_occurred', 'audit_log', ['occurred_at'], unique=False)
    op.create_table('heartbeats',
    sa.Column('component', sa.String(length=40), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('component', name=op.f('pk_heartbeats'))
    )
    op.create_table('jobs',
    sa.Column('type', sa.String(length=60), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('priority', sa.Integer(), server_default=sa.text('100'), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default=sa.text('5'), nullable=False),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('locked_by', sa.String(length=100), nullable=True),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=200), nullable=True),
    sa.Column('uses_llm', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'running', 'done', 'failed', 'waiting_llm', 'cancelled')", name=op.f('ck_jobs_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_jobs_idempotency_key'))
    )
    op.create_index('ix_jobs_ready', 'jobs', ['status', 'run_after'], unique=False)
    op.create_table('llm_calls',
    sa.Column('backend', sa.String(length=30), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=True),
    sa.Column('purpose', sa.String(length=60), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('prompt_hash', sa.String(length=64), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_calls'))
    )
    op.create_table('memory_entries',
    sa.Column('category', sa.String(length=30), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('applies_to_agent_key', sa.String(length=40), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by', sa.String(length=10), nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("category IN ('sop', 'company_rule', 'owner_preference', 'procedure', 'contact_note')", name=op.f('ck_memory_entries_category')),
    sa.CheckConstraint("created_by IN ('user', 'agent')", name=op.f('ck_memory_entries_created_by')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memory_entries'))
    )
    op.create_table('notifications',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_notifications'))
    )
    op.create_table('settings',
    sa.Column('key', sa.String(length=60), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_settings'))
    )
    op.create_table('users',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('whatsapp_number', sa.String(length=20), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('approval_pin_hash', sa.String(length=255), nullable=True),
    sa.Column('last_inbound_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('owner', 'manager', 'viewer')", name=op.f('ck_users_role')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('whatsapp_number', name=op.f('uq_users_whatsapp_number'))
    )
    op.create_table('tasks',
    sa.Column('ref', sa.String(length=20), server_default=sa.text("'T-' || nextval('task_ref_seq')"), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('priority', sa.String(length=10), nullable=False),
    sa.Column('assigned_agent_id', sa.UUID(), nullable=True),
    sa.Column('created_by_user_id', sa.UUID(), nullable=True),
    sa.Column('source_message_id', sa.UUID(), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('parent_task_id', sa.UUID(), nullable=True),
    sa.Column('result_summary', sa.Text(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("priority IN ('urgent', 'high', 'normal', 'low')", name=op.f('ck_tasks_priority')),
    sa.CheckConstraint("status IN ('new', 'queued', 'in_progress', 'waiting_approval', 'waiting_owner', 'blocked', 'done', 'cancelled', 'failed')", name=op.f('ck_tasks_status')),
    sa.ForeignKeyConstraint(['assigned_agent_id'], ['agents.id'], name=op.f('fk_tasks_assigned_agent_id_agents')),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_tasks_created_by_user_id_users')),
    sa.ForeignKeyConstraint(['parent_task_id'], ['tasks.id'], name=op.f('fk_tasks_parent_task_id_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tasks')),
    sa.UniqueConstraint('ref', name=op.f('uq_tasks_ref'))
    )
    op.create_index(op.f('ix_tasks_due_at'), 'tasks', ['due_at'], unique=False)
    op.create_index('ix_tasks_status_priority', 'tasks', ['status', 'priority'], unique=False)
    op.create_table('approvals',
    sa.Column('ref', sa.String(length=20), server_default=sa.text("'A-' || nextval('approval_ref_seq')"), nullable=False),
    sa.Column('task_id', sa.UUID(), nullable=True),
    sa.Column('requested_by_agent_id', sa.UUID(), nullable=True),
    sa.Column('action_type', sa.String(length=80), nullable=False),
    sa.Column('action_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('risk_level', sa.String(length=10), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('confirmation_code', sa.String(length=8), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_by_user_id', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('execution_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("risk_level IN ('low', 'medium', 'high', 'critical')", name=op.f('ck_approvals_risk_level')),
    sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'execution_failed')", name=op.f('ck_approvals_status')),
    sa.ForeignKeyConstraint(['decided_by_user_id'], ['users.id'], name=op.f('fk_approvals_decided_by_user_id_users')),
    sa.ForeignKeyConstraint(['requested_by_agent_id'], ['agents.id'], name=op.f('fk_approvals_requested_by_agent_id_agents')),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_approvals_task_id_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approvals')),
    sa.UniqueConstraint('ref', name=op.f('uq_approvals_ref'))
    )
    op.create_index(op.f('ix_approvals_status'), 'approvals', ['status'], unique=False)
    op.create_table('files',
    sa.Column('storage_key', sa.String(length=255), nullable=False),
    sa.Column('original_name', sa.String(length=255), nullable=True),
    sa.Column('mime_type', sa.String(length=100), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('uploaded_by_user_id', sa.UUID(), nullable=True),
    sa.Column('related_task_id', sa.UUID(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('whatsapp', 'agent', 'upload')", name=op.f('ck_files_source')),
    sa.ForeignKeyConstraint(['related_task_id'], ['tasks.id'], name=op.f('fk_files_related_task_id_tasks')),
    sa.ForeignKeyConstraint(['uploaded_by_user_id'], ['users.id'], name=op.f('fk_files_uploaded_by_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_files')),
    sa.UniqueConstraint('storage_key', name=op.f('uq_files_storage_key'))
    )
    op.create_index(op.f('ix_files_related_task_id'), 'files', ['related_task_id'], unique=False)
    op.create_index(op.f('ix_files_sha256'), 'files', ['sha256'], unique=False)
    op.create_table('task_events',
    sa.Column('task_id', sa.UUID(), nullable=False),
    sa.Column('event_type', sa.String(length=30), nullable=False),
    sa.Column('actor_type', sa.String(length=10), nullable=False),
    sa.Column('actor_id', sa.String(length=64), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("actor_type IN ('user', 'agent', 'system')", name=op.f('ck_task_events_actor_type')),
    sa.CheckConstraint("event_type IN ('created', 'status_changed', 'note', 'assigned', 'approval_requested', 'approval_decided', 'file_attached', 'result_added', 'updated', 'reminder_sent')", name=op.f('ck_task_events_event_type')),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_task_events_task_id_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_events'))
    )
    op.create_index('ix_task_events_task_created', 'task_events', ['task_id', 'created_at'], unique=False)
    op.create_table('messages',
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('provider_message_id', sa.String(length=255), nullable=True),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('media_file_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('related_task_id', sa.UUID(), nullable=True),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("direction IN ('inbound', 'outbound')", name=op.f('ck_messages_direction')),
    sa.CheckConstraint("status IN ('received', 'processed', 'held', 'queued', 'sent', 'delivered', 'read', 'failed', 'ignored')", name=op.f('ck_messages_status')),
    sa.ForeignKeyConstraint(['media_file_id'], ['files.id'], name=op.f('fk_messages_media_file_id_files')),
    sa.ForeignKeyConstraint(['related_task_id'], ['tasks.id'], name=op.f('fk_messages_related_task_id_tasks')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_messages_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages')),
    sa.UniqueConstraint('provider_message_id', name=op.f('uq_messages_provider_message_id'))
    )
    op.create_index('ix_messages_user_created', 'messages', ['user_id', 'created_at'], unique=False)

    op.create_foreign_key(
        "fk_tasks_source_message_id_messages", "tasks", "messages", ["source_message_id"], ["id"]
    )

    # Append-only tables: block UPDATE and DELETE at the database level.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_modification() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
    """)
    for table in ("audit_log", "task_events"):
        op.execute(f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION forbid_modification();
        """)

    agents = sa.table(
        "agents",
        sa.column("key", sa.String), sa.column("display_name", sa.String),
        sa.column("status", sa.String), sa.column("description", sa.Text),
        sa.column("permissions", postgresql.JSONB), sa.column("config", postgresql.JSONB),
    )
    op.bulk_insert(agents, AGENTS)
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", postgresql.JSONB))
    op.bulk_insert(settings, [{"key": k, "value": v} for k, v in DEFAULT_SETTINGS.items()])


def downgrade() -> None:
    op.drop_constraint("fk_tasks_source_message_id_messages", "tasks", type_="foreignkey")
    for table in ("audit_log", "task_events"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index('ix_messages_user_created', table_name='messages')
    op.drop_table('messages')
    op.drop_index('ix_task_events_task_created', table_name='task_events')
    op.drop_table('task_events')
    op.drop_index(op.f('ix_files_sha256'), table_name='files')
    op.drop_index(op.f('ix_files_related_task_id'), table_name='files')
    op.drop_table('files')
    op.drop_index(op.f('ix_approvals_status'), table_name='approvals')
    op.drop_table('approvals')
    op.drop_index('ix_tasks_status_priority', table_name='tasks')
    op.drop_index(op.f('ix_tasks_due_at'), table_name='tasks')
    op.drop_table('tasks')
    op.drop_table('users')
    op.drop_table('settings')
    op.drop_table('notifications')
    op.drop_table('memory_entries')
    op.drop_table('llm_calls')
    op.drop_index('ix_jobs_ready', table_name='jobs')
    op.drop_table('jobs')
    op.drop_table('heartbeats')
    op.drop_index('ix_audit_log_occurred', table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_table('agents')
    op.execute("DROP FUNCTION IF EXISTS forbid_modification()")
    for seq in ("task_ref_seq", "approval_ref_seq", "error_ref_seq"):
        op.execute(f"DROP SEQUENCE IF EXISTS {seq}")
