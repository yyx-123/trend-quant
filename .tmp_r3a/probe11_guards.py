import sqlite3, tempfile, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from data.storage.db import Database
tmp = Path(tempfile.mkdtemp(prefix="r3g-")); db = Database(tmp / "t.db")
con = sqlite3.connect(db.db_path); con.row_factory = sqlite3.Row
trigs = [r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")]
print("触发器总数 =", len(trigs))
print("engine 相关 =", [t for t in trigs if "engine" in t])
print("portfolio 相关 =", [t for t in trigs if "portfolio" in t])
# 直写证据表：填一条 run + nav + fills，然后改写
with db.connect() as c:
    c.execute("""INSERT INTO engine_runs (run_id, kind, strategy_ref, config_hash,
                 resolved_config_yaml, run_params_json, data_version, engine_version, git_hash,
                 status) VALUES ('R1','backtest','s','h','y','{}',1,'1','g','finished')""")
    c.execute("""INSERT INTO engine_daily_nav (run_id, date, cash, positions_value, equity)
                 VALUES ('R1','2024-01-02',0,0,1000)""")
    c.execute("""INSERT INTO engine_fills (run_id, order_id, symbol, fill_date, base_price,
                 fill_price, quantity, commission, stamp_tax, fee_total, cash_after)
                 VALUES ('R1','O1','X','2024-01-02',10,10,100,0,0,0,900)""")
def attempt(sql, params=()):
    try:
        with db.connect() as c:
            c.execute(sql, params)
        return "ALLOWED"
    except Exception as exc:
        return f"BLOCKED: {type(exc).__name__}: {str(exc)[:60]}"
print("改写 engine_daily_nav.equity ->", attempt("UPDATE engine_daily_nav SET equity=999999 WHERE run_id='R1'"))
print("删除 engine_fills 行        ->", attempt("DELETE FROM engine_fills WHERE run_id='R1'"))
print("改写 engine_fills.quantity  ->", attempt("UPDATE engine_fills SET quantity=1 WHERE run_id='R1'"))
print("改写 engine_runs.config_hash->", attempt("UPDATE engine_runs SET config_hash='zzz' WHERE run_id='R1'"))
print("删除 research_verdicts 行   ->", attempt("DELETE FROM research_verdicts WHERE id='VX'"))
print("改写 strategy_versions      ->", attempt("UPDATE portfolio_strategy_versions SET config_yaml='z' WHERE id='nope'"))
