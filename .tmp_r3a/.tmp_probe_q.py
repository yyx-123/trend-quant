import sqlite3
con = sqlite3.connect("file:data/trend_quant.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for r in con.execute("SELECT key, value, updated_at FROM app_config WHERE key LIKE 'research%' ORDER BY key"):
    print(dict(r))
print("--- counts ---")
for t in ("research_topics","research_experiments","research_runs","research_verdicts","research_sessions","holdout_tokens","module_drafts","portfolio_strategies","portfolio_strategy_versions","portfolio_live_lists","engine_runs","engine_fills","gateway_audit"):
    try:
        print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    except Exception as e:
        print(t, "ERR", e)
con.close()
