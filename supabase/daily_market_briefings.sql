create table if not exists public.daily_market_briefings (
    id uuid primary key default gen_random_uuid(),
    briefing_date date not null,
    session_type text not null check (session_type in ('morning', 'evening')),
    title text not null,
    tone text,
    topline text,
    briefing_markdown text not null,
    briefing_payload jsonb not null default '{}'::jsonb,
    source_summary jsonb not null default '{}'::jsonb,
    generated_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (briefing_date, session_type)
);

create index if not exists daily_market_briefings_date_idx
    on public.daily_market_briefings (briefing_date desc, session_type);

alter table public.daily_market_briefings enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'daily_market_briefings'
          and policyname = 'daily_market_briefings_read_all'
    ) then
        create policy "daily_market_briefings_read_all"
            on public.daily_market_briefings
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'daily_market_briefings'
          and policyname = 'daily_market_briefings_write_all'
    ) then
        create policy "daily_market_briefings_write_all"
            on public.daily_market_briefings
            for all
            using (true)
            with check (true);
    end if;
end $$;
