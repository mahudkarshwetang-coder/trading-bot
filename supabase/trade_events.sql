create table if not exists public.trade_events (
    id uuid primary key default gen_random_uuid(),
    signal_id uuid,
    account text,
    ticker text not null,
    action_type text,
    event_type text not null,
    status text,
    quantity numeric,
    price numeric,
    realized_pnl numeric,
    unrealized_pnl numeric,
    source text not null default 'bot',
    note text,
    occurred_at timestamptz not null default now(),
    created_at timestamptz not null default now()
);

create index if not exists trade_events_ticker_time_idx
    on public.trade_events (ticker, occurred_at desc);

create index if not exists trade_events_signal_idx
    on public.trade_events (signal_id);

create index if not exists trade_events_type_time_idx
    on public.trade_events (event_type, occurred_at desc);

alter table public.trade_events enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'trade_events'
          and policyname = 'trade_events_read_all'
    ) then
        create policy "trade_events_read_all"
            on public.trade_events
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'trade_events'
          and policyname = 'trade_events_write_all'
    ) then
        create policy "trade_events_write_all"
            on public.trade_events
            for all
            using (true)
            with check (true);
    end if;
end $$;
