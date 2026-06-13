create table if not exists public.system_status (
    component text primary key,
    status text not null,
    detail text,
    mode text,
    market_session text,
    run_id text,
    started_at timestamptz,
    finished_at timestamptz,
    heartbeat_at timestamptz not null default now(),
    error text,
    metadata jsonb not null default '{}'::jsonb
);

create index if not exists system_status_heartbeat_idx
    on public.system_status (heartbeat_at desc);

create index if not exists system_status_status_idx
    on public.system_status (status, heartbeat_at desc);

grant select, insert, update, delete on public.system_status to anon, authenticated;

alter table public.system_status enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'system_status'
          and policyname = 'system_status_read_all'
    ) then
        create policy "system_status_read_all"
            on public.system_status
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'system_status'
          and policyname = 'system_status_insert_all'
    ) then
        create policy "system_status_insert_all"
            on public.system_status
            for insert
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'system_status'
          and policyname = 'system_status_update_all'
    ) then
        create policy "system_status_update_all"
            on public.system_status
            for update
            using (true)
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'system_status'
          and policyname = 'system_status_delete_all'
    ) then
        create policy "system_status_delete_all"
            on public.system_status
            for delete
            using (true);
    end if;
end $$;
