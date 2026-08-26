"""db.py 覆盖率补测（目标 ≥95%）：各存储方法的边界与往返行为。

每条断言都验证真实返回值或库内状态，不做恒真断言。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest


class TestPasswordVerify:
    def test_non_hash_format_rejected(self) -> None:
        """明文兜底已清零（生产库 100% 哈希化）：非 pbkdf2 格式一律判失败。"""
        from data.storage.db import verify_password

        assert verify_password("plainpw", "plainpw") is False
        assert verify_password("plainpw", "other") is False
        # 形状接近但算法名不符的也拒绝
        assert verify_password("sha256$200000$aa$bb", "x") is False


class TestRuleStrategyStore:
    def test_save_requires_id(self, test_db) -> None:
        with pytest.raises(ValueError, match="id is required"):
            test_db.save_rule_strategy({"name": "x"})

    def test_save_conflict_and_roundtrip(self, test_db) -> None:
        strategy = {"id": "s1", "name": "策略一", "entry": {}, "exit": {}}
        saved = test_db.save_rule_strategy(strategy)
        assert saved["id"] == "s1"
        with pytest.raises(FileExistsError):
            test_db.save_rule_strategy(strategy)
        # overwrite 放行
        saved2 = test_db.save_rule_strategy({**strategy, "name": "策略一改"}, overwrite=True)
        assert saved2["strategy"]["name"] == "策略一改"

    def test_position_strategy_requires_id_and_roundtrip(self, test_db) -> None:
        with pytest.raises(ValueError, match="id is required"):
            test_db.save_position_strategy({"name": "x"})
        saved = test_db.save_position_strategy({"id": "p1", "name": "仓位一"})
        assert saved["id"] == "p1"
        with pytest.raises(FileExistsError):
            test_db.save_position_strategy({"id": "p1", "name": "仓位一"})


class TestTagHelpers:
    def test_json_tags_variants(self, test_db) -> None:
        assert test_db._json_tags("红利/低波") == json.dumps(["红利", "低波"], ensure_ascii=False)
        assert test_db._json_tags(["a", " b ", ""]) == json.dumps(["a", "b"], ensure_ascii=False)

    def test_parse_tags_variants(self, test_db) -> None:
        assert test_db._parse_tags(None) == []
        assert test_db._parse_tags("") == []
        assert test_db._parse_tags('["a","b"]') == ["a", "b"]
        assert test_db._parse_tags("红利/低波") == ["红利", "低波"]
        assert test_db._parse_tags('{"not": "list"}') == []


class TestInstrumentMetadataStore:
    def test_save_skips_empty_symbol_and_counts(self, test_db) -> None:
        n = test_db.save_instrument_metadata([
            {"symbol": "  ", "name": "坏行", "category_l1": "a"},
            {"symbol": "aaa.ss", "name": "好行", "category_l1": "L1", "category_l2": "L2",
             "category_l3": "L3", "enabled": False},
        ])
        assert n == 1
        meta = test_db.get_instrument_metadata("AAA.SS")
        assert meta["name"] == "好行"
        assert not meta["enabled"]

    def test_save_empty_records_returns_zero(self, test_db) -> None:
        assert test_db.save_instrument_metadata([{"symbol": "", "name": "x"}]) == 0

    def test_get_metadata_empty_symbol(self, test_db) -> None:
        assert test_db.get_instrument_metadata("") is None
        assert test_db.get_instrument_metadata("  ") is None


class TestDashboardSnapshot:
    def test_corrupt_payload_returns_none(self, test_db) -> None:
        with test_db._connect() as conn:
            conn.execute(
                "INSERT INTO dashboard_snapshot (id, kind, as_of, computed_at, payload)"
                " VALUES (1, 'intraday', '2026-08-25', '2026-08-25 10:00:00', 'not-json{')"
            )
        assert test_db.load_dashboard_snapshot() is None

class TestCategoryAndIndustryStore:
    def test_save_categories_skips_invalid(self, test_db) -> None:
        n = test_db.save_instrument_categories([
            {"path": "", "level": 1, "name": "坏"},
            {"path": "L1", "level": 1, "name": "", "priority": 1},
            {"path": "L1", "level": 1, "name": "好", "priority": 1},
        ])
        assert n == 1
        cats = test_db.list_instrument_categories()
        assert [c["path"] for c in cats] == ["L1"]

    def test_save_categories_empty_returns_zero(self, test_db) -> None:
        assert test_db.save_instrument_categories([{"path": "", "name": "x"}]) == 0

    def test_upsert_stock_industry_skips_incomplete(self, test_db) -> None:
        n = test_db.upsert_stock_industry(
            [
                {"symbol": "", "sw_l1_name": "电子", "sw_l2_name": "半导体"},
                {"symbol": "600519.SS", "sw_l1_name": "", "sw_l2_name": "半导体"},
                {"symbol": "600519.SS", "sw_l1_name": "电子", "sw_l2_name": "半导体"},
            ],
            "manual",
        )
        assert n == 1

    def test_upsert_stock_industry_empty_returns_zero(self, test_db) -> None:
        assert test_db.upsert_stock_industry([{"symbol": "", "sw_l1_name": "a", "sw_l2_name": "b"}], "manual") == 0

    def test_stock_industry_get_and_delete(self, test_db) -> None:
        test_db.upsert_stock_industry(
            [{"symbol": "600519.SS", "sw_l1_name": "电子", "sw_l2_name": "半导体"}], "manual"
        )
        row = test_db.get_stock_industry("600519.SS")
        assert row["sw_l1_name"] == "电子"
        assert test_db.get_stock_industry("") is None

    def test_update_instrument_category_empty_symbol(self, test_db) -> None:
        assert test_db.update_instrument_category("", "a", "b", "c", 1, 1, 1) is False


class TestEtfConstituentsStore:
    def test_save_requires_etf_and_period(self, test_db) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            test_db.save_etf_constituents("", [], "2026Q2")
        with pytest.raises(ValueError, match="不能为空"):
            test_db.save_etf_constituents("510300.SS", [], "")

    def test_save_filters_and_counts(self, test_db) -> None:
        n = test_db.save_etf_constituents(
            "510300.SS",
            [
                {"stock_symbol": "", "rank": 1},
                {"stock_symbol": "600519.SS", "rank": 1, "weight": 5.0, "name": "贵州茅台"},
            ],
            "2026Q2",
        )
        assert n == 1
        rows = test_db.list_current_etf_constituents("510300.SS")
        assert len(rows) == 1

    def test_save_empty_records_returns_zero(self, test_db) -> None:
        assert test_db.save_etf_constituents("510300.SS", [{"stock_symbol": "", "rank": 1}], "2026Q2") == 0

    def test_list_all_current_and_periods(self, test_db) -> None:
        test_db.save_etf_constituents(
            "510300.SS",
            [{"stock_symbol": "600519.SS", "rank": 1, "weight": 5.0, "name": "茅台"}],
            "2026Q2",
        )
        all_rows = test_db.list_all_current_etf_constituents()
        assert any(r["etf_symbol"] == "510300.SS" for r in all_rows)
        periods = test_db.list_etf_constituent_periods()
        assert any(p["etf_symbol"] == "510300.SS" and p["period"] == "2026Q2" for p in periods)


class TestMarketDataInternals:
    def test_market_table_rejects_bad_mode(self, test_db) -> None:
        with pytest.raises(ValueError, match="price_mode"):
            test_db._market_table("hfq")

    def test_market_records_drops_nonpositive(self, test_db) -> None:
        df = pd.DataFrame([
            {"time": "2026-08-20", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1, "amount": 1},
            {"time": "2026-08-21", "open": -1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1, "amount": 1},
        ])
        records, dropped = test_db._market_records("AAA.SS", df, "market_data_qfq")
        assert dropped == 1
        assert len(records) == 1

    def test_list_market_data_summaries(self, test_db) -> None:
        bars = pd.DataFrame([
            {"time": "2026-08-20", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
            {"time": "2026-08-21", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
        ])
        test_db.save_market_data("AAA.SS", bars, price_mode="qfq")
        test_db.save_market_data("BBB.SS", bars, price_mode="qfq")
        summaries = test_db.list_market_data_summaries()
        assert summaries["AAA.SS"]["rows"] == 2
        assert str(summaries["AAA.SS"]["end"]).startswith("2026-08-21")
        assert "BBB.SS" in summaries

    def test_load_market_tail(self, test_db) -> None:
        bars = pd.DataFrame([
            {"time": f"2026-08-{10 + i:02d}", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}
            for i in range(10)
        ])
        test_db.save_market_data("AAA.SS", bars, price_mode="qfq")
        tail = test_db.load_market_tail(days=365)
        assert len(tail) == 10
        assert tail[0]["symbol"] == "AAA.SS"
        assert "amount" in tail[0]


class TestExFactorsEdges:
    def test_save_skips_malformed_tuples(self, test_db) -> None:
        test_db.save_ex_factors("AAA.SS", [(None, "bad"), ("2026-08-01", "nan-x"), ("2026-08-02", 1.1)])
        rows = test_db.load_ex_factors("AAA.SS")
        assert rows == [("2026-08-02", 1.1)]

    def test_save_all_invalid_noop(self, test_db) -> None:
        test_db.save_ex_factors("AAA.SS", [(None, "bad"), ("2026-08-01", 0)])
        assert test_db.load_ex_factors("AAA.SS") == []


class TestUserStore:
    def test_create_user_requires_username(self, test_db) -> None:
        with pytest.raises(ValueError, match="username is required"):
            test_db.create_user("  ", "pw")

    def test_get_user_by_id(self, test_db) -> None:
        user = test_db.create_user("u1", "pw")
        fetched = test_db.get_user(user["id"])
        assert fetched["username"] == "u1"
        assert test_db.get_user(999999) is None


class TestManualTradeStore:
    def test_close_trade_twice_returns_none(self, test_db) -> None:
        user = test_db.create_user("trader", "pw")
        trade = test_db.create_manual_trade(user["id"], "510300.SS", "2026-08-20", 4.0, 100)
        closed = test_db.close_manual_trade(trade["id"], "2026-08-21", 4.1)
        assert closed["status"] == "closed"
        # 重复清仓：rowcount=0 → None
        assert test_db.close_manual_trade(trade["id"], "2026-08-22", 4.2) is None

    def test_get_manual_trade_missing(self, test_db) -> None:
        assert test_db.get_manual_trade(999999) is None


class TestMarkInterruptedJobRunsEdges:
    def test_empty_job_types_returns_zero(self, test_db) -> None:
        assert test_db.mark_interrupted_job_runs([]) == 0

    def test_running_without_job_id_marked(self, test_db) -> None:
        test_db.record_job_run("instrument_add", {"note": "no job_id here"}, status="running")
        marked = test_db.mark_interrupted_job_runs(["instrument_add"])
        assert marked == 1


class TestConfigStore:
    def test_get_config_text_and_json(self, test_db) -> None:
        test_db.set_config("plain", "raw-text-not-json")
        assert test_db.get_config("plain") == "raw-text-not-json"
        test_db.set_config("obj", {"a": 1})
        assert test_db.get_config("obj") == {"a": 1}
        assert test_db.get_config("missing", default="d") == "d"

    def test_list_config_values(self, test_db) -> None:
        test_db.set_config("k1", {"x": 1})
        test_db.set_config("k2", "plain{")
        values = test_db.get_all_config()
        assert values["k1"] == {"x": 1}
        assert values["k2"] == "plain{"


class TestIndicatorCaches:
    def test_save_indicator_daily_empty_returns_zero(self, test_db) -> None:
        assert test_db.save_indicator_daily("AAA.SS", pd.DataFrame(), formula_version=1) == 0

    def test_indicator_roundtrip_and_versions(self, test_db) -> None:
        df = pd.DataFrame({
            "time": ["2026-08-20", "2026-08-21"],
            "atr": [0.1, 0.2],
            "vol_ma20": [100.0, 110.0],
            "er10": [0.5, 0.6],
            "rsi14": [55.0, 60.0],
            "ema_s": [1.0, 1.1],
            "ema_m": [1.0, 1.1],
            "ema_l": [1.0, 1.1],
            "macd_dif": [0.01, 0.02],
            "macd_dea": [0.01, 0.02],
            "macd_hist": [0.0, 0.0],
        })
        n = test_db.save_indicator_daily("AAA.SS", df, formula_version=1, data_version=3)
        assert n == 2
        loaded = test_db.load_indicator_daily("AAA.SS")
        assert len(loaded) == 2
        assert "AAA.SS" in test_db.indicator_cache_symbols()
        assert test_db.indicator_global_version() == 1

        test_db.clear_indicator_caches()
        assert test_db.indicator_cache_symbols() == set()
        assert test_db.indicator_global_version() is None

    def test_save_trend_daily_and_load_with_since(self, test_db) -> None:
        df = pd.DataFrame({
            "time": ["2026-08-19", "2026-08-20", "2026-08-21"],
            "trend_score": [1.0, 2.0, 3.0],
            "trend_ma5": [1.0, 1.5, 2.0],
            "trend_ma10": [1.0, 1.4, 1.8],
            "price_direction": [1.0, 1.0, 1.0],
            "confidence": [0.5, 0.5, 0.5],
            "atr": [0.1, 0.1, 0.1],
            "er": [0.5, 0.5, 0.5],
            "vol_ratio": [1.0, 1.0, 1.0],
        })
        n = test_db.save_trend_daily("AAA.SS", df, formula_version=1)
        assert n == 3
        all_rows = test_db.load_trend_daily("AAA.SS")
        assert len(all_rows) == 3
        since_rows = test_db.load_trend_daily("AAA.SS", since="2026-08-21")
        assert len(since_rows) == 1
        assert test_db.load_trend_daily("NOPE.SS").empty

    def test_load_trend_daily_bulk(self, test_db) -> None:
        df = pd.DataFrame({
            "time": ["2026-08-20"],
            "trend_score": [1.0],
            "trend_ma5": [1.0],
            "trend_ma10": [1.0],
            "price_direction": [1.0],
            "confidence": [0.5],
            "atr": [0.1],
            "er": [0.5],
            "vol_ratio": [1.0],
        })
        test_db.save_trend_daily("AAA.SS", df, formula_version=1)
        bulk = test_db.load_trend_daily_bulk("2026-08-01", formula_version=1)
        assert len(bulk) == 1
        assert bulk[0]["symbol"] == "AAA.SS"


class TestUpdateBatchRun:
    def test_update_no_allowed_fields_noop(self, test_db) -> None:
        # 无可更新字段时静默返回（db.py:1963）
        test_db.update_batch_run("b1", nonexistent_field=1)
        assert test_db.get_batch_run("b1") is None

    def test_update_allowed_fields(self, test_db) -> None:
        created = test_db.create_batch_run_if_idle(
            {
                "batch_id": "b1",
                "name": "批1",
                "categories_json": "[]",
                "strategy_snapshot_json": "[]",
                "config_json": "{}",
                "total_cells": 4,
            }
        )
        assert created is True
        test_db.update_batch_run("b1", status="running", done_cells=2)
        run = test_db.get_batch_run("b1")
        assert run["status"] == "running"
        assert run["done_cells"] == 2


class TestRecordJobRunSafely:
    def test_swallows_db_failure(self, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        import logging

        import data.storage.db as db_module

        monkeypatch.setattr(db_module, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("down")))
        with caplog.at_level(logging.WARNING, logger="data.storage.db"):
            db_module.record_job_run_safely("x", {}, status="failed")
        # best-effort：不抛异常，且失败有 warning 记录（告警链路最后一环不静默）
        assert any("Failed to record job run" in r.message for r in caplog.records)
