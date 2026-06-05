alter table public.bot_settings
    add column if not exists force_broker_sync boolean not null default false;
