"""标签归属校验专用路由：签收权限、冲突确认、轮次派单等领域动作集中在此。

通用资源路由只承担查询/登记/状态流转；带权限判断的归属动作不允许走通用
actions 入口，统一在本路由校验提交人身份后分派给 TagOwnershipService。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.table_catalog import TableCatalogService
from app.services.tag_ownership import TagOwnershipService

router = APIRouter(prefix="/api/ownership", tags=["标签归属校验"])


def _ensure_ok(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok"):
        return result
    status_map = {
        "forbidden": 403,
        "invalid_layer": 400,
        "not_found": 404,
        "no_round": 409,
        "duplicate_round": 409,
    }
    raise HTTPException(
        status_code=status_map.get(result.get("code"), 400),
        detail=result.get("message", "归属校验操作失败"),
    )


class CatalogRegisterPayload(BaseModel):
    table_name: str
    db_name: str
    row_count: int = 0
    columns: list[dict[str, Any]] = Field(default_factory=list)


class CatalogReEnterPayload(BaseModel):
    table_name: str
    db_name: str


class PermissionPayload(BaseModel):
    table_name: str
    operator: str


class SignoffPayload(BaseModel):
    table_name: str
    layer: str
    operator: str


class ConfirmPayload(BaseModel):
    table_name: str
    layer: str
    operator: str
    basis: str = ""


class DispatchPayload(BaseModel):
    group: str
    round_id: str | None = None


class RoundOpenPayload(BaseModel):
    round_label: str = ""


@router.post("/catalog/register")
def register_catalog(payload: CatalogRegisterPayload) -> dict[str, Any]:
    """表目录入库：保持库名，自动生成库名分层的归属声明。"""
    columns = payload.columns or [
        {"字段名称": "id", "数据类型": "BIGINT", "可为空": False, "是否主键": True},
        {"字段名称": "biz_code", "数据类型": "VARCHAR(255)", "可为空": True, "是否主键": False},
    ]
    result = TableCatalogService().register_table_with_columns(
        payload.table_name, payload.db_name, columns, payload.row_count
    )
    return {"ok": True, "登记结果": result}


@router.post("/catalog/re-enter")
def re_enter_catalog(payload: CatalogReEnterPayload) -> dict[str, Any]:
    """重新进入目录：刷新自动分层来源记录，已有的签收/确认结论保留。"""
    result = TableCatalogService().trigger_ownership_validation(
        payload.table_name, payload.db_name
    )
    return _ensure_ok(result)


@router.post("/permission/grant")
def grant_permission(payload: PermissionPayload) -> dict[str, Any]:
    return _ensure_ok(
        TagOwnershipService().grant_confirm_permission(
            payload.table_name, payload.operator
        )
    )


@router.post("/signoff")
def submit_signoff(payload: SignoffPayload) -> dict[str, Any]:
    """业务签收另一层归属；未取得该表确认权限的提交返回 403。"""
    return _ensure_ok(
        TagOwnershipService().submit_manual_signoff(
            payload.table_name, payload.layer, payload.operator
        )
    )


@router.post("/confirm")
def confirm_ownership(payload: ConfirmPayload) -> dict[str, Any]:
    """确认归属并回写分类标签清单，标签服务持久化标签归属数据。"""
    return _ensure_ok(
        TagOwnershipService().confirm_ownership(
            payload.table_name, payload.layer, payload.operator, payload.basis
        )
    )


@router.get("/conflicts")
def list_conflicts() -> dict[str, Any]:
    service = TagOwnershipService()
    conflicts = service.list_conflicts()
    return {"ok": True, "冲突数": len(conflicts), "items": conflicts}


@router.get("/tables/{table_name}")
def ownership_detail(table_name: str) -> dict[str, Any]:
    return _ensure_ok(TagOwnershipService().ownership_snapshot(table_name))


@router.post("/rounds/open")
def open_round(payload: RoundOpenPayload) -> dict[str, Any]:
    return TagOwnershipService().open_inspection_round(payload.round_label)


@router.post("/rounds/dispatch")
def dispatch_round(payload: DispatchPayload) -> dict[str, Any]:
    """负责组触发一次规则选择；同轮重复触发或重复领取返回 409。"""
    return _ensure_ok(
        TagOwnershipService().dispatch_inspection(payload.group, payload.round_id)
    )


@router.get("/rounds")
def round_detail(round_id: str | None = None) -> dict[str, Any]:
    return _ensure_ok(TagOwnershipService().round_detail(round_id))
