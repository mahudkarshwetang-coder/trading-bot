create table if not exists public.strategy_optimizer_runs (
    id uuid primary key default gen_random_uuid(),
    run_at timestamptz not null default now(),
    lookback_days integer not null,
    preferred_horizon text not null,
    sample_count integer not null default 0,
    evaluated_count integer not null default 0,
    current_settings jsonb not null default '{}'::jsonb,
    recommended_settings jsonb not null default '{}'::jsonb,
    channel_stats jsonb not null default '{}'::jsonb,
    category_stats jsonb not null default '{}'::jsonb,
    trade_event_stats jsonb not null default '{}'::jsonb,
    broker_position_stats jsonb not null default '{}'::jsonb,
    post_trade_lessons jsonb not null default '{}'::jsonb,
    recommendations jsonb not null default '[]'::jsonb,
    applied boolean not null default false,
    apply_result jsonb not null default '{}'::jsonb,
    source text not null default 'strategy_optimizer',
    updated_at timestamptz not null default now()
);

create index if not exists strategy_optimizer_runs_run_at_idx
    on public.strategy_optimizer_runs (run_at desc);

alter table public.strategy_optimizer_runs
    add column if not exists broker_position_stats jsonb not null default '{}'::jsonb;

alter table public.strategy_optimizer_runs enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'strategy_optimizer_runs'
          and policyname = 'strategy_optimizer_runs_read_all'
    ) then
        create policy "strategy_optimizer_runs_read_all"
            on public.strategy_optimizer_runs
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'strategy_optimizer_runs'
          and policyname = 'strategy_optimizer_runs_write_all'
    ) then
        create policy "strategy_optimizer_runs_write_all"
            on public.strategy_optimizer_runs
            for all
            using (true)
            with check (true);
    end if;
end $$;
