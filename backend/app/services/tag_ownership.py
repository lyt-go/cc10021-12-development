"""标签归属校验服务：处理自动分层与人工签收的冲突，形成模板选择巡检目标的标签归属数据。

domain_chain: 标签归属 (tag_ownership)
  └─ 入库 → 表目录按库名自动分层 (来源记录: 自动分层)
  └─ 签收 → 业务组人工分层签收 (来源记录: 业务签收)
  └─ 确认 → 回写分类标签清单 (tag_classify)
  └─ 派单 → 规则模板按生效范围安排巡检 (dq_template / dq_check_run)

同一张表的两份归属声明（库名分层 vs 业务签收）各自保留来源记录；只有取得
该表确认权限的提交才能改动签收结果；归属确认前模板派单按双归属处理，确认后
按唯一确认归属处理，并保证同一轮次同一确认归属不会被两个组各执行一次。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 归属分层 -> 负责组的分配规则：两个组日常都按这个归属规则领取巡检目标。
LAYER_GROUPS: dict[str, str] = {
    "ODS层": "明细层治理组",
    "DWD层": "明细层治理组",
    "DWS层": "应用层治理组",
    "ADS层": "应用层治理组",
    "DIM层": "公共维度组",
}

# 库名 -> 自动分层的关键词规则，与 tag_classify 的库名识别保持一致。
LAYER_KEYWORDS: list[tuple[str, str]] = [
    ("ods", "ODS层"),
    ("dwd", "DWD层"),
    ("dws", "DWS层"),
    ("ads", "ADS层"),
    ("dim", "DIM层"),
]

SOURCE_AUTO = "自动分层"
SOURCE_MANUAL = "业务签收"
SOURCE_CONFIRM = "归属确认"

VALID_LAYERS = list(LAYER_GROUPS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def layer_from_db_name(db_name: str) -> str:
    """按库名推导自动分层，识别不出时返回未知层。"""
    name = (db_name or "").lower()
    for keyword, layer in LAYER_KEYWORDS:
        if keyword in name:
            return layer
    return "未知层"


class TagOwnershipService(BaseModuleService):
    MODULE = "tag_ownership"
    ENTITY = "标签归属"
    OPERATION_TERM = "归属校验"
    LIST_FIELDS = [
        "归属编号", "表名称", "所属库名", "自动分层", "业务签收",
        "确认归属", "确认依据", "负责组", "签收人", "确认人", "更新时间",
    ]
    REQUIRED_FIELDS = ["表名称", "所属库名"]
    STATUS_ORDER = ["待签收", "待确认", "已确认", "归属冲突"]
    ACTION_RULES = {
        "提交签收": "待确认",
        "确认归属": "已确认",
        "标记冲突": "归属冲突",
    }
    NEGATIVE_ACTIONS = ["标记冲突"]

    # ------------------------------------------------------------------
    # 存储辅助
    # ------------------------------------------------------------------
    def _ownership_rows(self) -> list[dict[str, Any]]:
        return store.rows(self.MODULE)

    def _source_rows(self) -> list[dict[str, Any]]:
        return store.rows("tag_ownership_source")

    def _round_rows(self) -> list[dict[str, Any]]:
        return store.rows("inspection_round")

    def _claim_rows(self) -> list[dict[str, Any]]:
        return store.rows("inspection_claim")

    def _permission_rows(self) -> list[dict[str, Any]]:
        return store.rows("tag_ownership_permission")

    def _next_id(self, rows: list[dict[str, Any]]) -> int:
        return max((int(r.get("id", 0)) for r in rows), default=0) + 1

    def find_by_table(self, table_name: str) -> dict[str, Any] | None:
        for row in self._ownership_rows():
            if row.get("表名称") == table_name:
                return row
        return None

    def has_permission(self, table_name: str, operator: str) -> bool:
        return any(
            row.get("表名称") == table_name and row.get("提交人") == operator
            for row in self._permission_rows()
        )

    def _append_source(self, table_name: str, layer: str, source: str,
                       operator: str, note: str) -> None:
        """自动分层和确认归属各留来源记录，业务签收同样留痕。"""
        rows = self._source_rows()
        rows.append({
            "id": self._next_id(rows),
            "来源编号": f"OWN_S-{len(rows) + 1:04d}",
            "表名称": table_name,
            "归属分层": layer,
            "来源类型": source,
            "提交人": operator,
            "来源说明": note,
            "记录时间": _now(),
            "status": "已留痕",
            "pending": False,
            "abnormal": False,
        })

    def _recompute(self, entry: dict[str, Any]) -> dict[str, Any]:
        """根据两份归属声明重新计算冲突态与负责组。"""
        auto_layer = entry.get("自动分层", "未知层")
        manual_layer = entry.get("业务签收", "")
        confirmed = entry.get("确认归属", "")
        conflict = bool(manual_layer) and manual_layer != auto_layer
        entry["归属冲突"] = conflict
        entry["abnormal"] = conflict and not confirmed
        if confirmed:
            status = "已确认"
        elif conflict:
            status = "归属冲突"
        elif manual_layer:
            status = "待确认"
        else:
            status = "待签收"
        entry["status"] = status
        entry["pending"] = status != "已确认"
        effective = confirmed or manual_layer or auto_layer
        entry["负责组"] = LAYER_GROUPS.get(effective, "待分配")
        entry["更新时间"] = _now()
        return entry

    def _refresh_catalog(self, entry: dict[str, Any]) -> None:
        """把归属校验结论同步回表目录的归属校验字段。"""
        for catalog in store.rows("table_catalog"):
            if catalog.get("表名称") == entry.get("表名称"):
                if entry.get("确认归属"):
                    text = (
                        f"已确认:{entry['确认归属']}({entry.get('负责组', '待分配')})"
                    )
                elif entry.get("归属冲突"):
                    text = f"冲突:自动{entry.get('自动分层')}/签收{entry.get('业务签收')}"
                elif entry.get("业务签收"):
                    text = f"待确认:签收{entry['业务签收']}"
                else:
                    text = f"待签收:自动{entry.get('自动分层', '未知层')}"
                catalog["归属校验"] = text
                catalog["abnormal"] = entry["abnormal"]
                break

    # ------------------------------------------------------------------
    # 自动分层：表目录入库 / 重新进入目录时触发
    # ------------------------------------------------------------------
    def submit_auto_claim(self, table_name: str, db_name: str) -> dict[str, Any]:
        """目录按库名生成自动分层声明，并保留自动分层来源记录。"""
        layer = layer_from_db_name(db_name)
        rows = self._ownership_rows()
        entry = self.find_by_table(table_name)
        created = entry is None
        if entry is None:
            entry = {
                "id": self._next_id(rows),
                "归属编号": f"OWN_A-{len(rows) + 1:04d}",
                "表名称": table_name,
                "所属库名": db_name,
                "自动分层": layer,
                "业务签收": "",
                "确认归属": "",
                "确认依据": "",
                "负责组": LAYER_GROUPS.get(layer, "待分配"),
                "签收人": "",
                "确认人": "",
                "更新时间": _now(),
                "归属冲突": False,
                "status": "待签收",
                "pending": True,
                "abnormal": False,
            }
            rows.append(entry)
        else:
            # 重新进入目录：库名可能调整，自动分层以最新库名为准。
            entry["所属库名"] = db_name
            entry["自动分层"] = layer
        self._append_source(
            table_name, layer, SOURCE_AUTO, "目录服务",
            f"按库名{db_name}自动分层为{layer}",
        )
        self._recompute(entry)
        self._refresh_catalog(entry)
        return {
            "ok": True,
            "表名称": table_name,
            "自动分层": layer,
            "是否新建": created,
            "当前状态": entry["status"],
            "归属冲突": entry["归属冲突"],
        }

    # ------------------------------------------------------------------
    # 签收权限
    # ------------------------------------------------------------------
    def grant_confirm_permission(self, table_name: str, operator: str) -> dict[str, Any]:
        """授予提交人对该表签收/确认结果的改动权限。"""
        if not table_name or not operator:
            return {"ok": False, "message": "表名称和提交人不能为空"}
        rows = self._permission_rows()
        for row in rows:
            if row.get("表名称") == table_name and row.get("提交人") == operator:
                return {"ok": True, "message": "权限已存在，无需重复授予", "表名称": table_name}
        rows.append({
            "id": self._next_id(rows),
            "权限编号": f"OWN_P-{len(rows) + 1:04d}",
            "表名称": table_name,
            "提交人": operator,
            "授权时间": _now(),
            "status": "已授权",
            "pending": False,
            "abnormal": False,
        })
        return {"ok": True, "message": f"已授予{operator}对{table_name}的确认权限", "表名称": table_name}

    # ------------------------------------------------------------------
    # 人工签收
    # ------------------------------------------------------------------
    def submit_manual_signoff(self, table_name: str, layer: str, operator: str) -> dict[str, Any]:
        """业务签收提交另一层归属；未取得确认权限的提交不得越权改动签收结果。"""
        if not self.has_permission(table_name, operator):
            return {
                "ok": False,
                "code": "forbidden",
                "message": f"提交人{operator}未取得{table_name}的确认权限，不得改动签收结果",
            }
        if layer not in VALID_LAYERS:
            return {"ok": False, "code": "invalid_layer",
                    "message": f"归属分层必须是{'、'.join(VALID_LAYERS)}之一"}
        entry = self.find_by_table(table_name)
        if entry is None:
            return {"ok": False, "code": "not_found", "message": f"表{table_name}尚未进入目录归属校验"}

        if layer == entry.get("业务签收"):
            return {"ok": True, "message": "签收归属未变化，已保留原签收记录",
                    "表名称": table_name, "当前状态": entry["status"]}

        entry["业务签收"] = layer
        entry["签收人"] = operator
        # 已确认表被有权限的提交改签到另一层：签收结果可改，但确认结论需重新作出。
        if entry.get("确认归属") and entry["确认归属"] != layer:
            entry["确认归属"] = ""
            entry["确认依据"] = ""
            entry["确认人"] = ""
        self._append_source(
            table_name, layer, SOURCE_MANUAL, operator,
            f"业务人工签收归属为{layer}",
        )
        self._recompute(entry)
        self._refresh_catalog(entry)
        return {
            "ok": True,
            "表名称": table_name,
            "业务签收": layer,
            "当前状态": entry["status"],
            "归属冲突": entry["归属冲突"],
            "自动分层": entry.get("自动分层"),
        }

    # ------------------------------------------------------------------
    # 归属确认：结论回写分类标签清单
    # ------------------------------------------------------------------
    def confirm_ownership(self, table_name: str, layer: str, operator: str,
                          basis: str = "") -> dict[str, Any]:
        """确认唯一归属并持久化到标签服务，作为模板选择巡检目标的标签依据。"""
        if not self.has_permission(table_name, operator):
            return {
                "ok": False,
                "code": "forbidden",
                "message": f"提交人{operator}未取得{table_name}的确认权限，不得确认归属",
            }
        if layer not in VALID_LAYERS:
            return {"ok": False, "code": "invalid_layer",
                    "message": f"归属分层必须是{'、'.join(VALID_LAYERS)}之一"}
        entry = self.find_by_table(table_name)
        if entry is None:
            return {"ok": False, "code": "not_found", "message": f"表{table_name}尚未进入目录归属校验"}

        auto_layer = entry.get("自动分层", "未知层")
        manual_layer = entry.get("业务签收", "")
        if layer == auto_layer and manual_layer and layer == manual_layer:
            basis_text = basis or "自动分层与业务签收一致"
        elif layer == manual_layer:
            basis_text = basis or f"业务签收覆盖自动分层（自动:{auto_layer}）"
        elif layer == auto_layer:
            basis_text = basis or f"维持库名自动分层（签收:{manual_layer or '无'}）"
        else:
            basis_text = basis or "人工裁定归属"

        entry["确认归属"] = layer
        entry["确认依据"] = basis_text
        entry["确认人"] = operator
        self._append_source(table_name, layer, SOURCE_CONFIRM, operator, basis_text)
        self._recompute(entry)
        self._refresh_catalog(entry)

        tag_info = self._persist_tag(entry)
        return {
            "ok": True,
            "表名称": table_name,
            "确认归属": layer,
            "负责组": entry["负责组"],
            "确认依据": basis_text,
            "标签回写": tag_info,
        }

    def _persist_tag(self, entry: dict[str, Any]) -> dict[str, Any]:
        """归属确认结论回写分类标签清单，标签服务持久化标签归属数据。"""
        from app.services.tag_classify import TagClassifyService

        return TagClassifyService().persist_ownership_tag(
            table_name=entry["表名称"],
            layer=entry["确认归属"],
            group=entry["负责组"],
            basis=entry["确认依据"],
            confirmer=entry.get("确认人", ""),
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_conflicts(self) -> list[dict[str, Any]]:
        """返回自动分层与业务签收不一致且尚未确认的表。"""
        return [
            row for row in self._ownership_rows()
            if row.get("归属冲突") and not row.get("确认归属")
        ]

    def ownership_snapshot(self, table_name: str) -> dict[str, Any]:
        entry = self.find_by_table(table_name)
        if entry is None:
            return {"ok": False, "message": f"表{table_name}暂无归属数据"}
        sources = [
            row for row in self._source_rows() if row.get("表名称") == table_name
        ]
        return {
            "ok": True,
            "归属": dict(entry),
            "来源记录": sources,
            "有权限提交人": sorted({
                row["提交人"] for row in self._permission_rows()
                if row.get("表名称") == table_name
            }),
        }

    # ------------------------------------------------------------------
    # 巡检派单：规则模板按生效范围给负责组安排巡检，同轮同归属去重
    # ------------------------------------------------------------------
    def _pick_template(self, group: str) -> dict[str, Any] | None:
        """在已发布模板里挑选生效范围覆盖该负责组的模板。"""
        templates = store.rows("dq_template")
        published = [t for t in templates if t.get("status") == "已发布"]
        for tpl in published:
            scope = str(tpl.get("生效范围", ""))
            if not scope or group in scope or "全组" in scope:
                return tpl
        return published[0] if published else None

    def _current_round_id(self) -> str | None:
        rounds = self._round_rows()
        if not rounds:
            return None
        return max(rounds, key=lambda r: int(r.get("id", 0))).get("轮次编号")

    def dispatch_inspection(self, group: str, round_id: str | None = None) -> dict[str, Any]:
        """一个负责组触发一次规则选择：按归属领取表，同轮同表不重复领取。"""
        round_id = round_id or self._current_round_id()
        if not round_id or not any(
            r.get("轮次编号") == round_id for r in self._round_rows()
        ):
            return {"ok": False, "code": "no_round",
                    "message": "巡检轮次不存在，请先开启一轮巡检"}

        claims = self._claim_rows()
        claimed_keys = {
            (c.get("轮次编号"), c.get("表名称")) for c in claims
        }
        claimed_by_group = {
            (c.get("轮次编号"), c.get("负责组")) for c in claims
        }
        if (round_id, group) in claimed_by_group:
            return {"ok": False, "code": "duplicate_round",
                    "message": f"{group}在轮次{round_id}已触发过规则选择，同轮不得重复执行"}

        tpl = self._pick_template(group)
        picked: list[dict[str, Any]] = []
        skipped_dup: list[str] = []
        skipped_scope: list[str] = []

        for entry in self._ownership_rows():
            effective = entry.get("确认归属") or entry.get("业务签收") or entry.get("自动分层")
            target_group = LAYER_GROUPS.get(effective, "")
            if target_group != group:
                skipped_scope.append(entry["表名称"])
                continue
            if (round_id, entry["表名称"]) in claimed_keys:
                # 另一组已按同一确认归属在本轮领取：同轮不得执行两次。
                skipped_dup.append(entry["表名称"])
                continue
            tag_basis = entry.get("确认归属") or (
                f"待确认(自动{entry.get('自动分层')}/签收{entry.get('业务签收') or '无'})"
            )
            claim_id = self._next_id(claims)
            claims.append({
                "id": claim_id,
                "领取编号": f"OWN_C-{claim_id:04d}",
                "轮次编号": round_id,
                "归属编号": entry.get("归属编号"),
                "表名称": entry["表名称"],
                "负责组": group,
                "标签依据": tag_basis,
                "模板编号": tpl.get("模板编号") if tpl else "",
                "领取时间": _now(),
                "status": "已领取",
                "pending": False,
                "abnormal": False,
            })
            claimed_keys.add((round_id, entry["表名称"]))
            picked.append({
                "表名称": entry["表名称"],
                "标签依据": tag_basis,
                "模板编号": tpl.get("模板编号") if tpl else "",
            })
            self._write_check_run(round_id, group, entry, tpl)

        if tpl is not None:
            try:
                tpl["使用次数"] = str(int(str(tpl.get("使用次数", "0")) or 0) + 1)
            except ValueError:
                tpl["使用次数"] = "1"

        return {
            "ok": True,
            "轮次编号": round_id,
            "负责组": group,
            "模板编号": tpl.get("模板编号") if tpl else "",
            "领取表数": len(picked),
            "领取明细": picked,
            "同轮去重跳过": skipped_dup,
            "范围外跳过": len(skipped_scope),
        }

    def _write_check_run(self, round_id: str, group: str,
                         entry: dict[str, Any], tpl: dict[str, Any] | None) -> None:
        rows = store.rows("dq_check_run")
        new_id = self._next_id(rows)
        rows.append({
            "id": new_id,
            "任务编号": f"DQ_C-own-{new_id:04d}",
            "关联规则集": tpl.get("模板编号", "") if tpl else "未匹配模板",
            "数据范围": entry["表名称"],
            "Cron表达式": f"轮次{round_id}",
            "并行度": "2",
            "超时时间": "1800s",
            "失败阈值": "20%",
            "任务版本": f"归属派单-{group}",
            "status": "运行中",
            "pending": False,
            "abnormal": False,
        })

    def open_inspection_round(self, round_label: str = "") -> dict[str, Any]:
        """开启一轮巡检，两个组随后各触发一次规则选择。"""
        rounds = self._round_rows()
        new_id = self._next_id(rounds)
        round_id = round_label or f"ROUND-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        rounds.append({
            "id": new_id,
            "轮次编号": round_id,
            "开启时间": _now(),
            "轮次状态": "进行中",
            "status": "进行中",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "轮次编号": round_id, "message": "巡检轮次已开启，等待各负责组触发规则选择"}

    def round_detail(self, round_id: str | None = None) -> dict[str, Any]:
        round_id = round_id or self._current_round_id()
        if not round_id:
            return {"ok": False, "message": "暂无巡检轮次"}
        claims = [c for c in self._claim_rows() if c.get("轮次编号") == round_id]
        return {
            "ok": True,
            "轮次编号": round_id,
            "领取记录": claims,
            "按组统计": {
                group: sum(1 for c in claims if c.get("负责组") == group)
                for group in sorted({c.get("负责组") for c in claims})
            },
        }

    # ------------------------------------------------------------------
    # 通用动作入口加防护：签收/确认必须走带权限校验的领域方法
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        return None, (
            f"动作「{action}」必须通过归属校验专用接口执行（签收/确认需校验提交人权限），"
            "通用动作入口不允许改动签收结果"
        )

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "归属校验"
        snapshot["desc"] = "校验自动分层与人工签收冲突并确认标签归属"
        snapshot["artifact"] = "标签归属数据集"
        snapshot["artifact_desc"] = "供模板选择巡检目标的标签归属数据"
        rows = self._ownership_rows()
        snapshot["冲突未确认"] = sum(
            1 for row in rows if row.get("归属冲突") and not row.get("确认归属")
        )
        snapshot["已确认归属"] = sum(1 for row in rows if row.get("确认归属"))
        return snapshot
