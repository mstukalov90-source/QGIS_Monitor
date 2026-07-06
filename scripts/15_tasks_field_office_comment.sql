-- Office comment on field task snapshots (also applied at runtime by the plugin).
ALTER TABLE crm.tasks_field ADD COLUMN IF NOT EXISTS office_comment TEXT;
