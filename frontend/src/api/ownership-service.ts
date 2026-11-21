/**
 * 标签归属校验专用接口：签收权限、人工签收、归属确认、巡检轮次派单。
 * 与通用资源接口分开，因为这些动作需要提交人身份与权限校验，错误码按后端约定映射。
 */
import { request } from './client'

export type OwnershipRow = {
  id: number
  归属编号: string
  表名称: string
  所属库名: string
  自动分层: string
  业务签收: string
  确认归属: string
  确认依据: string
  负责组: string
  签收人: string
  确认人: string
  更新时间: string
  归属冲突: boolean
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type SourceRecord = {
  来源编号: string
  表名称: string
  归属分层: string
  来源类型: string
  提交人: string
  来源说明: string
  记录时间: string
}

export type ClaimRecord = {
  领取编号: string
  轮次编号: string
  表名称: string
  负责组: string
  标签依据: string
  模板编号: string
  领取时间: string
}

export type DispatchResult = {
  ok: boolean
  轮次编号: string
  负责组: string
  模板编号: string
  领取表数: number
  领取明细: { 表名称: string; 标签依据: string; 模板编号: string }[]
  同轮去重跳过: string[]
  范围外跳过: number
}

async function post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? '归属校验操作失败')
  }
  return payload as T
}

async function get<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(payload?.detail ?? '归属校验数据读取失败')
  }
  return (await response.json()) as T
}

export function registerCatalog(tableName: string, dbName: string, rowCount = 0) {
  return post<{ ok: boolean; 登记结果: Record<string, unknown> }>('/api/ownership/catalog/register', {
    table_name: tableName,
    db_name: dbName,
    row_count: rowCount,
  })
}

export function reEnterCatalog(tableName: string, dbName: string) {
  return post<{ ok: boolean; 自动分层: string; 当前状态: string }>('/api/ownership/catalog/re-enter', {
    table_name: tableName,
    db_name: dbName,
  })
}

export function grantPermission(tableName: string, operator: string) {
  return post<{ ok: boolean; message: string }>('/api/ownership/permission/grant', {
    table_name: tableName,
    operator,
  })
}

export function submitSignoff(tableName: string, layer: string, operator: string) {
  return post<{ ok: boolean; 当前状态: string; 归属冲突: boolean }>('/api/ownership/signoff', {
    table_name: tableName,
    layer,
    operator,
  })
}

export function confirmOwnership(tableName: string, layer: string, operator: string, basis = '') {
  return post<{ ok: boolean; 确认归属: string; 负责组: string; 确认依据: string }>(
    '/api/ownership/confirm',
    { table_name: tableName, layer, operator, basis },
  )
}

export function listConflicts() {
  return get<{ ok: boolean; 冲突数: number; items: OwnershipRow[] }>('/api/ownership/conflicts')
}

export function ownershipDetail(tableName: string) {
  return get<{
    ok: boolean
    归属: OwnershipRow
    来源记录: SourceRecord[]
    有权限提交人: string[]
  }>(`/api/ownership/tables/${encodeURIComponent(tableName)}`)
}

export function openRound(roundLabel = '') {
  return post<{ ok: boolean; 轮次编号: string; message: string }>('/api/ownership/rounds/open', {
    round_label: roundLabel,
  })
}

export function dispatchRound(group: string, roundId?: string) {
  return post<DispatchResult>('/api/ownership/rounds/dispatch', {
    group,
    round_id: roundId ?? null,
  })
}

export function roundDetail(roundId?: string) {
  const query = roundId ? `?round_id=${encodeURIComponent(roundId)}` : ''
  return get<{
    ok: boolean
    轮次编号: string
    领取记录: ClaimRecord[]
    按组统计: Record<string, number>
  }>(`/api/ownership/rounds${query}`)
}
