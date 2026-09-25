import sqlite3, json
con = sqlite3.connect("file:data/trend_quant.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
print("== engine_runs ==")
for r in con.execute("SELECT run_id,kind,strategy_ref,config_hash,data_version,engine_version,git_hash,started_at,finished_at,status,substr(resolved_config_yaml,1,40) y, run_params_json FROM engine_runs ORDER BY run_id"):
    print(dict(r))
print("== strategies ==")
for r in con.execute("SELECT * FROM portfolio_strategies ORDER BY id"):
    print(dict(r))
print("== versions ==")
for r in con.execute("SELECT id,strategy_id,version,config_hash,parent_version_id,experiment_id,created_by,created_at FROM portfolio_strategy_versions ORDER BY id"):
    print(dict(r))
con.close()
