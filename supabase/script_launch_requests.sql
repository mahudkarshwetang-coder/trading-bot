create table if not exists public.script_launch_requests (
    id uuid primary key default gen_random_uuid(),
    script_key text not null,
    requested_by text not null default 'signalcenter',
    status text not null default 'pending',
    pid integer,
    launcher_host text,
    error text,
    requested_at timestamptz not null default now(),
    launched_at timestamptz,
    updated_at timestamptz not null default now(),
    metadata jsonb not null default '{}'::jsonb,
    constraint script_launch_requests_status_check
        check (status in ('pending', 'launching', 'launched', 'error', 'cancelled')),
    constraint script_launch_requests_key_check
        check (script_key in (
            'execution-bridge',
            'master-engine',
            'training-engine',
            'experimental-engine',
            'pure-training-engine',
            'daily-cycle',
            'radar-scan',
            'earnings-radar',
            'llm-scanner',
            'context-enrichment',
            'category-refresh',
            'experimental-cycle',
            'experimental-build',
            'experimental-findings',
            'experimental-execute',
            'pure-training-cycle',
            'experimental-review',
            'pure-training-review',
            'broker-sync-loop',
            'tech-news-monitor',
            'health-check'
        ))
);

create index if not exists script_launch_requests_status_requested_idx
    on public.script_launch_requests (status, requested_at desc);

grant select, insert, update on table public.script_launch_requests to anon, authenticated, service_role;

alter table public.script_launch_requests enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'script_launch_requests'
          and policyname = 'script_launch_requests_read_all'
    ) then
        create policy "script_launch_requests_read_all"
            on public.script_launch_requests
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'script_launch_requests'
          and policyname = 'script_launch_requests_insert_all'
    ) then
        create policy "script_launch_requests_insert_all"
            on public.script_launch_requests
            for insert
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'script_launch_requests'
          and policyname = 'script_launch_requests_update_all'
    ) then
        create policy "script_launch_requests_update_all"
            on public.script_launch_requests
            for update
            using (true)
            with check (true);
    end if;
end $$;
