"""分类标签服务：基于表结构和元数据自动关联分类标签，驱动数据资产分类管理。

domain_chain: 分类标签 (tag_classify)
  └─ 关联 → 元数据采集 (metadata_crawler)
  └─ 标准化 → 规则模板 (dq_template)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class TagClassifyService(BaseModuleService):
    MODULE = "tag_classify"
    ENTITY = "分类标签"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["标签编号", "标签名称", "分类维度", "关联表名", "标签值", "分类依据", "生效时间", "标签版本"]
    REQUIRED_FIELDS = ["标签编号", "标签名称", "分类维度"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    def classify_tables_by_metadata(self, min_quality: float = 60.0) -> dict[str, Any]:
        """基于元数据对表进行自动分类，关联分类标签并标准化表分类体系。

        查询表目录和字段画像数据，按分层、存储和访问模式关联分类标签。
        分类结果写入标签存储，同时标准化分类规则供模板复用。
        """
        catalog_rows = store.rows("table_catalog")
        tag_rows = store.rows(self.MODULE)

        categories = {"ODS层": [], "DWS层": [], "ADS层": [], "DIM层": []}
        classified = 0

        for cat in catalog_rows:
            db_name = cat.get("所属库名", "")
            field_count = int(cat.get("字段数量", 0))
            row_count_str = cat.get("数据行数", "0")

            # 关联分类标签：根据库名分层
            if "ods" in db_name.lower():
                layer = "ODS层"
            elif "dws" in db_name.lower():
                layer = "DWS层"
            elif "ads" in db_name.lower():
                layer = "ADS层"
            elif "dim" in db_name.lower():
                layer = "DIM层"
            else:
                layer = "未知"

            # 存储分类
            storage = "大表" if any(
                int(x) > 10000000 for x in row_count_str.replace(",", "").split()
                if x.isdigit()
            ) else "小表"

            for tag_type, tag_value in [("分层", layer), ("存储量级", storage)]:
                new_tid = max((int(r.get("id", 0)) for r in tag_rows), default=0) + 1
                tag_rows.append({
                    "id": new_tid,
                    "标签编号": f"TAG_-{new_tid:04d}",
                    "标签名称": f"{cat.get('表名称', '')}_{tag_type}",
                    "分类维度": tag_type,
                    "关联表名": cat.get("表名称", ""),
                    "标签值": tag_value,
                    "分类依据": f"基于{db_name}来源和{field_count}字段自动分类",
                    "生效时间": datetime.now(timezone.utc).isoformat(),
                    "标签版本": "v1.0",
                    "status": "已生效",
                    "pending": False,
                    "abnormal": False,
                })
                classified += 1

            if layer != "未知":
                categories[layer].append(cat.get("表名称", ""))

        # 标准化分类规则写入模板服务
        self._sync_classification_rules()

        return {
            "分类时间": datetime.now(timezone.utc).isoformat(),
            "总分类标签数": classified,
            "覆盖表数": len(catalog_rows),
            "分层统计": {k: len(v) for k, v in categories.items() if v},
        }

    def persist_ownership_tag(self, table_name: str, layer: str, group: str,
                              basis: str, confirmer: str = "") -> dict[str, Any]:
        """持久化标签归属数据：归属确认结论回写分类标签清单。

        同一张表的「分层」标签只保留一条权威记录，确认结论覆盖自动分类结果；
        同时记录负责组与确认依据，作为规则模板选择巡检目标的标签依据。
        """
        tag_rows = store.rows(self.MODULE)
        existing = next(
            (row for row in tag_rows
             if row.get("关联表名") == table_name and row.get("分类维度") == "分层"),
            None,
        )
        now = datetime.now(timezone.utc).isoformat()
        if existing is not None:
            old_value = existing.get("标签值")
            existing["标签值"] = layer
            existing["分类依据"] = f"归属确认回写：{basis}；负责组：{group}"
            existing["生效时间"] = now
            existing["标签版本"] = self._bump_version(existing.get("标签版本", "v1"))
            existing["status"] = "已生效"
            existing["pending"] = False
            existing["abnormal"] = False
            existing["归属负责组"] = group
            existing["确认人"] = confirmer
            return {
                "标签编号": existing["标签编号"],
                "持久化方式": "更新",
                "原标签值": old_value,
                "标签值": layer,
            }

        new_tid = max((int(r.get("id", 0)) for r in tag_rows), default=0) + 1
        entry = {
            "id": new_tid,
            "标签编号": f"TAG_-{new_tid:04d}",
            "标签名称": f"{table_name}_分层",
            "分类维度": "分层",
            "关联表名": table_name,
            "标签值": layer,
            "分类依据": f"归属确认回写：{basis}；负责组：{group}",
            "生效时间": now,
            "标签版本": "v1.0",
            "归属负责组": group,
            "确认人": confirmer,
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        tag_rows.append(entry)
        return {
            "标签编号": entry["标签编号"],
            "持久化方式": "新建",
            "原标签值": "",
            "标签值": layer,
        }

    @staticmethod
    def _bump_version(version: Any) -> str:
        text = str(version or "v1")
        if "." in text:
            head, tail = text.rsplit(".", 1)
            if tail.isdigit():
                return f"{head}.{int(tail) + 1}"
        return f"{text}.1"

    def _sync_classification_rules(self) -> None:
        """标准化分类规则，将分类模式写入规则模板库供复用。"""
        template_rows = store.rows("dq_template")
        rules = [
            ("ODS层分类规则", "ODS", "ods_开头表自动归入ODS层"),
            ("ADS层分类规则", "ADS", "ads_开头表自动归入ADS层"),
            ("大表分类规则", "存储量级", "行数>1000W标记为大表"),
        ]
        for tpl_name, rule_type, desc in rules:
            new_tid = max((int(r.get("id", 0)) for r in template_rows), default=0) + 1
            template_rows.append({
                "id": new_tid,
                "模板编号": f"DQ_T-{new_tid:04d}",
                "模板名称": tpl_name,
                "规则类型": rule_type,
                "校验SQL": f"SELECT * FROM classification WHERE type='{rule_type}'",
                "参数列表": "[\"tag_layer\", \"storage_level\"]",
                "适用场景": desc,
                "使用次数": "1",
                "模板版本": "v1",
                "status": "已发布",
                "pending": False,
                "abnormal": False,
            })

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "关联"
        snapshot["desc"] = "基于元数据关联分类标签并标准化分类规则"
        snapshot["artifact"] = "分类标签数据集"
        snapshot["artifact_desc"] = "按分层和存储量级分类的标签数据集"
        return snapshot