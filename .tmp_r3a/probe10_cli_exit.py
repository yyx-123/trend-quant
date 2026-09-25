"""R3 probe 10: CLI 子命令的真实退出码/错误路径（subprocess 走真 CLI）。"""
import json, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/Scripts/python.exe")
tmp = Path(tempfile.mkdtemp(prefix="r3probe10-"))
dbp = tmp / "t.db"
# 先建库 + 种子（用 CLI 自己的 _service 路径）
sys.path.insert(0, str(ROOT / "src"))
from data.storage.db import Database
from portfolio.seed import seed_default_library
from portfolio.slots import REGISTRY, ensure_builtins
ensure_builtins(); db = Database(dbp); versions = seed_default_library(db, REGISTRY)

def run(*args):
    p = subprocess.run([PY, str(ROOT / "scripts/research_cli.py"), "--db", str(dbp), *args],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    return p.returncode, (p.stdout or "").strip()[:150], (p.stderr or "").strip()[-200:]

print("非法 JSON spec:", run("propose-experiment", "--topic", "T001", "--eval",
      "portfolio_backtest@1", "--spec", "{not json", "--hypothesis", "假设足够长的陈述"))
print("不存在的课题:", run("propose-experiment", "--topic", "T999", "--eval",
      "portfolio_backtest@1", "--spec", '{"base":"base-v1@1","diff":[]}',
      "--hypothesis", "假设足够长的陈述"))
print("不存在的实验 confirm:", run("confirm", "E9999", "--verdict", "confirmed", "--reasoning", "r"))
print("不存在的实验 rerun:", run("rerun", "E9999", "--run"))
print("不存在课题 conclude:", run("conclude", "T999", "--conclusion", "c"))
print("不存在课题 promote:", run("promote", "E9999", "--strategy-id", "x"))
print("非法 --verdict:", run("confirm", "E9999", "--verdict", "maybe", "--reasoning", "r")[0])
print("空 ledger:", run("ledger"))
print("topics:", run("topics"))
print("TMP", tmp)
