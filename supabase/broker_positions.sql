create table if not exists public.broker_positions (
    id uuid primary key default gen_random_uuid(),
    account text not null,
    ticker text not null,
    con_id bigint,
    sec_type text,
    exchange text,
    currency text default 'USD',
    quantity numeric not null default 0,
    avg_cost numeric,
    market_price numeric,
    market_value numeric,
    unrealized_pnl numeric,
    side text,
    source text not null default 'ibkr',
    is_open boolean not null default true,
    synced_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (account, ticker)
);

create index if not exists broker_positions_open_idx
    on public.broker_positions (account, is_open, ticker);

alter table public.broker_positions enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'broker_positions'
          and policyname = 'broker_positions_read_all'
    ) then
        create policy "broker_positions_read_all"
            on public.broker_positions
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'broker_positions'
          and policyname = 'broker_positions_write_all'
    ) then
        create policy "broker_positions_write_all"
            on public.broker_positions
            for all
            using (true)
            with check (true);
    end if;
end $$;
