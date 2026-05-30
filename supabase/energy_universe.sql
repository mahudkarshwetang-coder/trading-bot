create table if not exists public.energy_universe (
    ticker text primary key,
    company_name text not null,
    exchange text not null check (exchange in ('NASDAQ', 'NYSE')),
    sector text,
    industry text,
    category text not null,
    subcategory text,
    energy_theme text not null,
    energy_purity_score numeric(5, 2) not null default 0,
    market_cap bigint,
    country text,
    website text,
    description text,
    matched_keywords text[] not null default '{}',
    source text not null default 'energy_universe_builder',
    active boolean not null default true,
    first_seen timestamptz not null default now(),
    last_seen timestamptz not null default now(),
    last_updated timestamptz not null default now()
);

create index if not exists energy_universe_exchange_idx
    on public.energy_universe (exchange, active, ticker);

create index if not exists energy_universe_category_idx
    on public.energy_universe (category, subcategory, active);

create index if not exists energy_universe_theme_idx
    on public.energy_universe (energy_theme, active);

create index if not exists energy_universe_score_idx
    on public.energy_universe (energy_purity_score desc, ticker);

create index if not exists energy_universe_sector_idx
    on public.energy_universe (sector, industry);

alter table public.energy_universe enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'energy_universe'
          and policyname = 'energy_universe_read_all'
    ) then
        create policy "energy_universe_read_all"
            on public.energy_universe
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'energy_universe'
          and policyname = 'energy_universe_write_all'
    ) then
        create policy "energy_universe_write_all"
            on public.energy_universe
            for all
            using (true)
            with check (true);
    end if;
end $$;
