create table if not exists public.signal_journal (
    journal_id uuid primary key default gen_random_uuid(),
    signal_id uuid,
    created_at_utc timestamptz not null default now(),
    ticker text not null,
    action_type text not null,
    channel text,
    status text,
    confidence_score numeric,
    price_at_signal numeric,
    rsi numeric,
    sma_15 numeric,
    rvol numeric,
    bid numeric,
    ask numeric,
    investment_memo_excerpt text,
    price_after_15m numeric,
    return_15m_pct numeric,
    correct_15m boolean,
    evaluated_at_15m timestamptz,
    price_after_1h numeric,
    return_1h_pct numeric,
    correct_1h boolean,
    evaluated_at_1h timestamptz,
    price_after_1d numeric,
    return_1d_pct numeric,
    correct_1d boolean,
    evaluated_at_1d timestamptz,
    price_after_5d numeric,
    return_5d_pct numeric,
    correct_5d boolean,
    evaluated_at_5d timestamptz,
    updated_at timestamptz not null default now()
);

create unique index if not exists signal_journal_signal_id_unique_idx
    on public.signal_journal (signal_id)
    where signal_id is not null;

create index if not exists signal_journal_created_idx
    on public.signal_journal (created_at_utc desc);

create index if not exists signal_journal_channel_idx
    on public.signal_journal (channel, created_at_utc desc);

create index if not exists signal_journal_ticker_idx
    on public.signal_journal (ticker, created_at_utc desc);

alter table public.signal_journal enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'signal_journal'
          and policyname = 'signal_journal_read_all'
    ) then
        create policy "signal_journal_read_all"
            on public.signal_journal
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'signal_journal'
          and policyname = 'signal_journal_write_all'
    ) then
        create policy "signal_journal_write_all"
            on public.signal_journal
            for all
            using (true)
            with check (true);
    end if;
end $$;
