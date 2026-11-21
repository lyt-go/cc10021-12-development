"""标签归属校验链路：自动分层 vs 业务签收冲突、权限、确认回写、同轮去重派单。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.table_catalog import TableCatalogService
from app.services.tag_classify import TagClassifyService
from app.services.tag_ownership import (
    LAYER_GROUPS,
    TagOwnershipService,
    layer_from_db_name,
)
from app.store import store


@pytest.fixture(autouse=True)
def unique_tables():
    """每个用例使用独立表名，避免与其它链路共用内存仓库时互相干扰。"""
    TestOwnershipConflictChain._seq = getattr(TestOwnershipConflictChain, "_seq", 0) + 1
    yield f"own_{TestOwnershipConflictChain._seq}_"


class TestLayerFromDbName:
    def test_keyword_mapping(self):
        assert layer_from_db_name("dwd_warehouse") == "DWD层"
        assert layer_from_db_name("ads_mart") == "ADS层"
        assert layer_from_db_name("ods_source") == "ODS层"
        assert layer_from_db_name("oracle_biz") == "未知层"

    def test_group_assignment_rule(self):
        assert LAYER_GROUPS["DWD层"] == "明细层治理组"
        assert LAYER_GROUPS["ADS层"] == "应用层治理组"


class TestOwnershipConflictChain:
    def _register(self, table: str, db: str = "dwd_warehouse"):
        columns = [
            {"字段名称": "id", "数据类型": "BIGINT", "可为空": False, "是否主键": True},
            {"字段名称": "amt", "数据类型": "DECIMAL(18,2)", "可为空": True, "是否主键": False},
        ]
        return TableCatalogService().register_table_with_columns(table, db, columns, 1000)

    def test_catalog_register_keeps_db_name_and_creates_auto_claim(self, unique_tables):
        table = unique_tables + "dwd_order"
        result = self._register(table)
        assert result["注册表"] == table
        assert result["归属校验"]["自动分层"] == "DWD层"

        entry = TagOwnershipService().find_by_table(table)
        assert entry is not None
        assert entry["自动分层"] == "DWD层"
        assert entry["负责组"] == "明细层治理组"
        assert entry["status"] == "待签收"

        catalog = next(r for r in store.rows("table_catalog") if r["表名称"] == table)
        assert catalog["所属库名"] == "dwd_warehouse"
        assert "归属校验" in catalog and catalog["归属校验"].startswith("待签收")

    def test_dual_source_records_are_kept(self, unique_tables):
        table = unique_tables + "dual_source"
        self._register(table)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        own.submit_manual_signoff(table, "ADS层", "签收员")

        snap = own.ownership_snapshot(table)
        sources = {(s["来源类型"], s["归属分层"]) for s in snap["来源记录"]}
        assert ("自动分层", "DWD层") in sources
        assert ("业务签收", "ADS层") in sources

    def test_unauthorized_submit_cannot_change_signoff(self, unique_tables):
        table = unique_tables + "no_perm"
        self._register(table)
        own = TagOwnershipService()

        result = own.submit_manual_signoff(table, "ADS层", "未授权提交人")
        assert result["ok"] is False
        assert result["code"] == "forbidden"
        entry = own.find_by_table(table)
        assert entry["业务签收"] == ""
        assert entry["status"] == "待签收"

    def test_manual_signoff_creates_conflict(self, unique_tables):
        table = unique_tables + "conflict"
        self._register(table)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        result = own.submit_manual_signoff(table, "ADS层", "签收员")

        assert result["ok"] is True
        assert result["归属冲突"] is True
        assert result["当前状态"] == "归属冲突"
        assert table in [c["表名称"] for c in own.list_conflicts()]
        assert own.find_by_table(table)["abnormal"] is True

    def test_invalid_layer_rejected(self, unique_tables):
        table = unique_tables + "bad_layer"
        self._register(table)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        result = own.submit_manual_signoff(table, "不存在的层", "签收员")
        assert result["ok"] is False
        assert result["code"] == "invalid_layer"

    def test_confirm_writes_back_classification_tag(self, unique_tables):
        table = unique_tables + "confirm"
        self._register(table)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        own.submit_manual_signoff(table, "ADS层", "签收员")
        result = own.confirm_ownership(table, "ADS层", "签收员")

        assert result["ok"] is True
        assert result["负责组"] == "应用层治理组"
        assert result["标签回写"]["标签值"] == "ADS层"

        tag = next(
            t for t in store.rows("tag_classify")
            if t["关联表名"] == table and t["分类维度"] == "分层"
        )
        assert tag["标签值"] == "ADS层"
        assert tag["归属负责组"] == "应用层治理组"
        assert tag["status"] == "已生效"

        entry = own.find_by_table(table)
        assert entry["status"] == "已确认"
        assert entry["pending"] is False
        catalog = next(r for r in store.rows("table_catalog") if r["表名称"] == table)
        assert catalog["归属校验"].startswith("已确认:ADS层")

    def test_tag_persistence_upserts_single_layer_tag(self, unique_tables):
        table = unique_tables + "upsert"
        self._register(table)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        own.confirm_ownership(table, "DWD层", "签收员")
        own.confirm_ownership(table, "ADS层", "签收员")

        layer_tags = [
            t for t in store.rows("tag_classify")
            if t["关联表名"] == table and t["分类维度"] == "分层"
        ]
        assert len(layer_tags) == 1
        assert layer_tags[0]["标签值"] == "ADS层"

    def test_unauthorized_confirm_blocked(self, unique_tables):
        table = unique_tables + "confirm_no_perm"
        self._register(table)
        own = TagOwnershipService()
        result = own.confirm_ownership(table, "DWD层", "路人")
        assert result["ok"] is False
        assert result["code"] == "forbidden"
        assert own.find_by_table(table)["确认归属"] == ""

    def test_re_enter_catalog_refreshes_auto_but_keeps_confirmation(self, unique_tables):
        table = unique_tables + "reenter"
        catalog = TableCatalogService()
        self._register(table)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        own.submit_manual_signoff(table, "ADS层", "签收员")
        own.confirm_ownership(table, "ADS层", "签收员")

        result = catalog.trigger_ownership_validation(table, "dwd_warehouse")
        assert result["是否新建"] is False
        assert result["自动分层"] == "DWD层"

        entry = own.find_by_table(table)
        assert entry["确认归属"] == "ADS层"
        assert entry["status"] == "已确认"
        snap = own.ownership_snapshot(table)
        assert sum(1 for s in snap["来源记录"] if s["来源类型"] == "自动分层") == 2

    def test_resign_after_confirmation_resets_confirmation(self, unique_tables):
        table = unique_tables + "resign"
        self._register(table)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        own.confirm_ownership(table, "DWD层", "签收员")
        own.submit_manual_signoff(table, "ADS层", "签收员")

        entry = own.find_by_table(table)
        assert entry["确认归属"] == ""
        assert entry["业务签收"] == "ADS层"
        assert entry["status"] == "归属冲突"


class TestInspectionDispatch:
    def _confirmed_table(self, table: str, db: str, sign_layer: str | None):
        columns = [{"字段名称": "id", "数据类型": "BIGINT", "可为空": False}]
        TableCatalogService().register_table_with_columns(table, db, columns, 10)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        if sign_layer:
            own.submit_manual_signoff(table, sign_layer, "签收员")
            own.confirm_ownership(table, sign_layer, "签收员")
        else:
            own.confirm_ownership(table, layer_from_db_name(db), "签收员")
        return own

    def test_two_groups_each_trigger_once_without_double_execution(self, unique_tables):
        table = unique_tables + "two_groups"
        own = self._confirmed_table(table, "dwd_warehouse", "ADS层")
        round_id = "ROUND-" + unique_tables.rstrip("_")
        own.open_inspection_round(round_id)

        detail = own.dispatch_inspection("明细层治理组", round_id)
        app = own.dispatch_inspection("应用层治理组", round_id)

        detail_picked = [x for x in detail["领取明细"] if x["表名称"] == table]
        app_picked = [x for x in app["领取明细"] if x["表名称"] == table]
        assert detail_picked == []
        assert len(app_picked) == 1
        assert app_picked[0]["标签依据"] == "ADS层"

        claims = [
            c for c in store.rows("inspection_claim")
            if c["轮次编号"] == round_id and c["表名称"] == table
        ]
        assert len(claims) == 1
        check_runs = [r for r in store.rows("dq_check_run") if r["数据范围"] == table]
        assert len(check_runs) == 1

    def test_same_group_cannot_trigger_twice_in_one_round(self, unique_tables):
        table = unique_tables + "dup_trigger"
        own = self._confirmed_table(table, "ads_mart", None)
        round_id = "ROUND-" + unique_tables.rstrip("_") + "b"
        own.open_inspection_round(round_id)
        own.dispatch_inspection("应用层治理组", round_id)
        again = own.dispatch_inspection("应用层治理组", round_id)
        assert again["ok"] is False
        assert again["code"] == "duplicate_round"

    def test_unconfirmed_conflict_single_claim_and_tag_basis(self, unique_tables):
        table = unique_tables + "unconfirmed"
        columns = [{"字段名称": "id", "数据类型": "BIGINT", "可为空": False}]
        TableCatalogService().register_table_with_columns(table, "dwd_db", columns, 1)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员")
        own.submit_manual_signoff(table, "DWS层", "签收员")
        round_id = "ROUND-" + unique_tables.rstrip("_") + "c"
        own.open_inspection_round(round_id)

        app = own.dispatch_inspection("应用层治理组", round_id)
        detail = own.dispatch_inspection("明细层治理组", round_id)
        picked = [item for item in app["领取明细"] if item["表名称"] == table]
        assert len(picked) == 1
        assert "待确认" in picked[0]["标签依据"]
        assert all(item["表名称"] != table for item in detail["领取明细"])

        round_data = own.round_detail(round_id)
        table_claims = [
            c for c in round_data["领取记录"] if c["表名称"] == table
        ]
        assert len(table_claims) == 1
        assert round_data["按组统计"]["应用层治理组"] >= 1

    def test_dispatch_without_round_rejected(self):
        result = TagOwnershipService().dispatch_inspection(
            "明细层治理组", "ROUND-NEVER-" + str(id(object()))
        )
        assert result["ok"] is False
        assert result["code"] == "no_round"

    def test_generic_action_entry_cannot_modify_signoff(self, unique_tables):
        table = unique_tables + "generic_block"
        self._confirmed_table(table, "dwd_db", "ADS层")
        entry = TagOwnershipService().find_by_table(table)
        result, message = TagOwnershipService().run_action(entry["id"], "确认归属")
        assert result is None
        assert "专用接口" in message


class TestOwnershipSnapshot:
    def test_snapshot_contains_permissions_and_sources(self, unique_tables):
        table = unique_tables + "snapshot"
        columns = [{"字段名称": "id", "数据类型": "BIGINT", "可为空": False}]
        TableCatalogService().register_table_with_columns(table, "ods_db", columns, 1)
        own = TagOwnershipService()
        own.grant_confirm_permission(table, "签收员甲")
        own.grant_confirm_permission(table, "签收员乙")

        snap = own.ownership_snapshot(table)
        assert snap["ok"] is True
        assert set(snap["有权限提交人"]) == {"签收员甲", "签收员乙"}
        assert snap["归属"]["自动分层"] == "ODS层"
        assert len(snap["来源记录"]) == 1

    def test_domain_snapshot_metrics(self):
        snapshot = TagOwnershipService().domain_snapshot()
        assert snapshot["operation"] == "归属校验"
        assert "冲突未确认" in snapshot and "已确认归属" in snapshot
