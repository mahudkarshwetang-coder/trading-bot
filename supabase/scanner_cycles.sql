create table if not exists public.scanner_cycles (
    cycle_id text primary key,
    cycle_type text not null,
    status text not null,
    market_session text,
    mode text,
    started_at timestamptz not null,
    finished_at timestamptz,
    duration_seconds numeric,
    workflows jsonb not null default '[]'::jsonb,
    scanners jsonb not null default '[]'::jsonb,
    operations jsonb not null default '[]'::jsonb,
    phases jsonb not null default '[]'::jsonb,
    signal_summary jsonb not null default '{}'::jsonb,
    trade_event_summary jsonb not null default '{}'::jsonb,
    errors jsonb not null default '[]'::jsonb,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists scanner_cycles_started_idx
    on public.scanner_cycles (started_at desc);

create index if not exists scanner_cycles_type_time_idx
    on public.scanner_cycles (cycle_type, started_at desc);

create index if not exists scanner_cycles_status_time_idx
    on public.scanner_cycles (status, started_at desc);

grant select, insert, update, delete on public.scanner_cycles to anon, authenticated;

alter table public.scanner_cycles enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'scanner_cycles'
          and policyname = 'scanner_cycles_read_all'
    ) then
        create policy "scanner_cycles_read_all"
            on public.scanner_cycles
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'scanner_cycles'
          and policyname = 'scanner_cycles_insert_all'
    ) then
        create policy "scanner_cycles_insert_all"
            on public.scanner_cycles
            for insert
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'scanner_cycles'
          and policyname = 'scanner_cycles_update_all'
    ) then
        create policy "scanner_cycles_update_all"
            on public.scanner_cycles
            for update
            using (true)
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'scanner_cycles'
          and policyname = 'scanner_cycles_delete_all'
    ) then
        create policy "scanner_cycles_delete_all"
            on public.scanner_cycles
            for delete
            using (true);
    end if;
end $$;
