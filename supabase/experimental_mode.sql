alter table public.bot_settings
    add column if not exists experimental_mode_enabled boolean not null default false,
    add column if not exists force_experimental_run boolean not null default false,
    add column if not exists experimental_last_requested_at timestamptz,
    add column if not exists experimental_last_result jsonb;

alter table public.script_launch_requests
    drop constraint if exists script_launch_requests_key_check;

alter table public.script_launch_requests
    add constraint script_launch_requests_key_check
        check (script_key in (
            'execution-bridge',
            'master-engine',
            'training-engine',
            'experimental-engine',
            'pure-training-engine',
            'daily-cycle',
            'radar-scan',
            'earnings-radar',
            'llm-scanner',
            'context-enrichment',
            'category-refresh',
            'experimental-cycle',
            'pure-training-cycle',
            'experimental-review',
            'pure-training-review',
            'broker-sync-loop',
            'tech-news-monitor',
            'health-check'
        ));
