create table if not exists public.post_trade_reviews (
    id uuid primary key default gen_random_uuid(),
    signal_id uuid not null unique,
    ticker text not null,
    action_type text,
    channel text,
    signal_status text,
    entry_price numeric,
    exit_price numeric,
    pnl_pct numeric,
    outcome_horizon text,
    signal_confidence numeric,
    overall_outcome text,
    review_summary text,
    what_worked jsonb not null default '[]'::jsonb,
    what_failed jsonb not null default '[]'::jsonb,
    training_adjustments jsonb not null default '[]'::jsonb,
    llm_payload jsonb not null default '{}'::jsonb,
    source text not null default 'qwen_post_trade_review',
    reviewed_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists post_trade_reviews_reviewed_idx
    on public.post_trade_reviews (reviewed_at desc);

create index if not exists post_trade_reviews_ticker_idx
    on public.post_trade_reviews (ticker, reviewed_at desc);

alter table public.post_trade_reviews enable row level security;

do $$
begin
    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'post_trade_reviews'
          and policyname = 'post_trade_reviews_read_all'
    ) then
        create policy "post_trade_reviews_read_all"
            on public.post_trade_reviews
            for select
            using (true);
    end if;

    if not exists (
        select 1 from pg_policies
        where schemaname = 'public'
          and tablename = 'post_trade_reviews'
          and policyname = 'post_trade_reviews_write_all'
    ) then
        create policy "post_trade_reviews_write_all"
            on public.post_trade_reviews
            for all
            using (true)
            with check (true);
    end if;
end $$;
