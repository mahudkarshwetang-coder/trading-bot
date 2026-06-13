create table if not exists public.bot_runtime_config (
    id integer primary key default 1,
    source text not null default 'windows_bot',
    risk_settings jsonb not null default '{}'::jsonb,
    scanner_settings jsonb not null default '{}'::jsonb,
    session_settings jsonb not null default '{}'::jsonb,
    llm_settings jsonb not null default '{}'::jsonb,
    performance_settings jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now(),
    constraint bot_runtime_config_singleton check (id = 1)
);

create index if not exists bot_runtime_config_updated_idx
    on public.bot_runtime_config (updated_at desc);

grant select, insert, update, delete on public.bot_runtime_config to anon, authenticated;

alter table public.bot_runtime_config enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'bot_runtime_config'
          and policyname = 'bot_runtime_config_read_all'
    ) then
        create policy "bot_runtime_config_read_all"
            on public.bot_runtime_config
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'bot_runtime_config'
          and policyname = 'bot_runtime_config_insert_all'
    ) then
        create policy "bot_runtime_config_insert_all"
            on public.bot_runtime_config
            for insert
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'bot_runtime_config'
          and policyname = 'bot_runtime_config_update_all'
    ) then
        create policy "bot_runtime_config_update_all"
            on public.bot_runtime_config
            for update
            using (true)
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'bot_runtime_config'
          and policyname = 'bot_runtime_config_delete_all'
    ) then
        create policy "bot_runtime_config_delete_all"
            on public.bot_runtime_config
            for delete
            using (true);
    end if;
end $$;
