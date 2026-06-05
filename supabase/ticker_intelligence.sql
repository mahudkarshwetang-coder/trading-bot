create table if not exists public.ticker_intelligence (
    ticker text primary key,
    company_name text,
    exchange text,
    sector text,
    industry text,
    market_cap numeric,
    quote_price numeric,
    quote_change_pct numeric,
    volume numeric,
    avg_volume numeric,
    beta numeric,
    trailing_pe numeric,
    forward_pe numeric,
    rsi_14 numeric,
    return_1d_pct numeric,
    return_1m_pct numeric,
    short_shares numeric,
    short_ratio numeric,
    short_pct_float numeric,
    short_pct_outstanding numeric,
    short_interest_as_of date,
    short_interest_summary text,
    options_expiry date,
    put_call_oi_ratio numeric,
    put_call_volume_ratio numeric,
    options_sentiment text,
    market_news_sentiment numeric,
    market_news_count integer not null default 0,
    market_news_items jsonb not null default '[]'::jsonb,
    public_chatter_sentiment numeric,
    public_chatter_count integer not null default 0,
    public_chatter_items jsonb not null default '[]'::jsonb,
    tech_news_sentiment numeric,
    tech_news_count integer not null default 0,
    tech_news_items jsonb not null default '[]'::jsonb,
    ibkr_news_sentiment numeric,
    ibkr_news_count integer not null default 0,
    ibkr_news_items jsonb not null default '[]'::jsonb,
    signal_bias_score numeric,
    signal_count integer not null default 0,
    signal_summary jsonb not null default '{}'::jsonb,
    sec_snapshot jsonb not null default '{}'::jsonb,
    overall_sentiment_score numeric,
    overall_sentiment_label text,
    source_summary jsonb not null default '{}'::jsonb,
    generated_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.ticker_intelligence
    add column if not exists ibkr_news_sentiment numeric,
    add column if not exists ibkr_news_count integer not null default 0,
    add column if not exists ibkr_news_items jsonb not null default '[]'::jsonb;

create index if not exists ticker_intelligence_updated_idx
    on public.ticker_intelligence (updated_at desc);

create index if not exists ticker_intelligence_sentiment_idx
    on public.ticker_intelligence (overall_sentiment_score desc nulls last);

grant select on public.ticker_intelligence to anon, authenticated;

alter table public.ticker_intelligence enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'ticker_intelligence'
          and policyname = 'ticker_intelligence_read_all'
    ) then
        create policy "ticker_intelligence_read_all"
            on public.ticker_intelligence
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'ticker_intelligence'
          and policyname = 'ticker_intelligence_write_all'
    ) then
        create policy "ticker_intelligence_write_all"
            on public.ticker_intelligence
            for all
            using (true)
            with check (true);
    end if;
end $$;
