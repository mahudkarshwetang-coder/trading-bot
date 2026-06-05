create table if not exists public.live_holdings (
    id uuid primary key default gen_random_uuid(),
    ticker text not null,
    company_name text,
    quantity numeric not null default 0,
    average_cost numeric not null default 0,
    broker text not null default 'wealthsimple',
    notes text,
    active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint live_holdings_ticker_unique unique (ticker),
    constraint live_holdings_quantity_positive check (quantity > 0),
    constraint live_holdings_average_cost_positive check (average_cost > 0)
);

create index if not exists live_holdings_active_ticker_idx
    on public.live_holdings (active, ticker);

create index if not exists live_holdings_updated_idx
    on public.live_holdings (updated_at desc);

grant select, insert, update, delete on public.live_holdings to anon, authenticated;

alter table public.live_holdings enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'live_holdings'
          and policyname = 'live_holdings_read_all'
    ) then
        create policy "live_holdings_read_all"
            on public.live_holdings
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'live_holdings'
          and policyname = 'live_holdings_insert_all'
    ) then
        create policy "live_holdings_insert_all"
            on public.live_holdings
            for insert
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'live_holdings'
          and policyname = 'live_holdings_update_all'
    ) then
        create policy "live_holdings_update_all"
            on public.live_holdings
            for update
            using (true)
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'live_holdings'
          and policyname = 'live_holdings_delete_all'
    ) then
        create policy "live_holdings_delete_all"
            on public.live_holdings
            for delete
            using (true);
    end if;
end $$;
