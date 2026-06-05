from config import (
    PERFORMANCE_GAMING_CPU_BUDGET_PCT,
    PERFORMANCE_GAMING_EXTERNAL_PRIORITY,
    PERFORMANCE_GAMING_MAX_LLM_ITEMS,
    PERFORMANCE_GAMING_MAX_LLM_TICKERS,
    PERFORMANCE_GAMING_MAX_WORKERS,
    PERFORMANCE_GAMING_PROCESS_PRIORITY,
)
from performance_governor import current_performance_profile, describe_performance_profile


def main():
    profile = current_performance_profile(force_refresh=True)
    print("Performance Governor Status")
    print("=" * 32)
    print(f"Mode: {'gaming/quiet' if profile.active else 'normal'}")
    print(f"Reason: {describe_performance_profile(profile)}")
    if profile.active:
        print(f"Gaming CPU budget: {PERFORMANCE_GAMING_CPU_BUDGET_PCT:g}%")
        print(f"Process priority: python={PERFORMANCE_GAMING_PROCESS_PRIORITY}, ollama={PERFORMANCE_GAMING_EXTERNAL_PRIORITY}")
        print(
            "Gaming caps: "
            f"workers={PERFORMANCE_GAMING_MAX_WORKERS}, "
            f"news_llm_items={PERFORMANCE_GAMING_MAX_LLM_ITEMS}, "
            f"llm_tickers={PERFORMANCE_GAMING_MAX_LLM_TICKERS}"
        )
    if profile.matched_processes:
        print(f"Matched processes: {', '.join(profile.matched_processes)}")
    if profile.cpu_pct is not None:
        print(f"CPU load: {profile.cpu_pct:.0f}%")
    if profile.free_memory_gb is not None:
        print(f"Free RAM: {profile.free_memory_gb:.1f}GB")


if __name__ == "__main__":
    main()
