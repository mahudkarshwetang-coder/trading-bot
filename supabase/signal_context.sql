create table if not exists public.signal_context (
    id uuid primary key default gen_random_uuid(),
    signal_id uuid not null unique,
    ticker text not null,
    broker_quantity numeric,
    broker_avg_cost numeric,
    broker_position_side text,
    sell_allowed boolean not null default false,
    no_short_block_reason text,
    quote_price numeric,
    quote_bid numeric,
    quote_ask numeric,
    quote_spread numeric,
    quote_prev_close numeric,
    quote_volume numeric,
    quote_source text,
    quote_at timestamptz,
    latest_filing_type text,
    latest_filing_date date,
    latest_filing_title text,
    latest_filing_url text,
    sec_risk_flags text[] not null default '{}',
    sec_risk_score numeric not null default 0,
    catalyst_summary text,
    macro_regime text,
    macro_summary text,
    context_score numeric,
    source_summary jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now(),
    created_at timestamptz not null default now()
);

create index if not exists signal_context_signal_idx
    on public.signal_context (signal_id);

create index if not exists signal_context_ticker_idx
    on public.signal_context (ticker);

create index if not exists signal_context_updated_idx
    on public.signal_context (updated_at desc);

alter table public.signal_context enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'signal_context'
          and policyname = 'signal_context_read_all'
    ) then
        create policy "signal_context_read_all"
            on public.signal_context
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'signal_context'
          and policyname = 'signal_context_write_all'
    ) then
        create policy "signal_context_write_all"
            on public.signal_context
            for all
            using (true)
            with check (true);
    end if;
end $$;
