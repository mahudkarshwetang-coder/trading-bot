alter table public.bot_settings
    add column if not exists force_master_scanner boolean not null default false;
