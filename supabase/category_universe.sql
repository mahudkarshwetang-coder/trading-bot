create table if not exists public.category_universe (
    id uuid primary key default gen_random_uuid(),
    ticker text not null,
    company_name text not null,
    exchange text,
    sector text,
    industry text,
    category text not null,
    theme text not null,
    category_score numeric not null,
    market_cap numeric,
    average_volume numeric,
    country text,
    website text,
    description text,
    matched_keywords text[] not null default '{}',
    source text not null default 'category_universe_builder:yfinance',
    active boolean not null default true,
    last_seen timestamptz not null default now(),
    last_updated timestamptz not null default now()
);

create unique index if not exists category_universe_ticker_category_theme_idx
    on public.category_universe (ticker, category, theme);

create index if not exists category_universe_category_score_idx
    on public.category_universe (category, category_score desc, market_cap desc);

create index if not exists category_universe_ticker_idx
    on public.category_universe (ticker);

alter table public.category_universe enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'category_universe'
          and policyname = 'category_universe_read_all'
    ) then
        create policy "category_universe_read_all"
            on public.category_universe
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'category_universe'
          and policyname = 'category_universe_write_all'
    ) then
        create policy "category_universe_write_all"
            on public.category_universe
            for all
            using (true)
            with check (true);
    end if;
end $$;
