"""Daily News ETL pipeline.

Replaces the agentic "model spams Tavily" pattern with a deterministic
extract-transform-load pipeline:

    sources (RSS/API/HTML) → normalize → dedup → rule_filter →
    rank → llm_batch_classify → markdown render

LLM is called ONCE per digest run on a ~30-item compact JSON, not
in a multi-turn loop. Cost is bounded and predictable.

Entry point: `pipeline.run_daily_news_pipeline(date, profile)`.
"""
