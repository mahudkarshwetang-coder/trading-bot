alter table public.bot_settings
    add column if not exists pure_training_mode_enabled boolean not null default false,
    add column if not exists force_pure_training_run boolean not null default false,
    add column if not exists pure_training_last_requested_at timestamptz,
    add column if not exists pure_training_last_result jsonb;
