<template>
  <section class="page" data-module="tag_ownership">
    <header class="page-head">
      <div>
        <h2>标签归属校验</h2>
        <p class="page-desc">
          库名自动分层与业务人工签收各留来源记录；确认权限控制签收改动，确认结论回写分类标签，
          规则模板按生效范围给负责组安排巡检，同轮同一归属不重复执行。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">归属记录</span>
        <strong class="stat-value">{{ rows.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">冲突未确认</span>
        <strong class="stat-value">{{ conflictRows.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已确认归属</span>
        <strong class="stat-value">{{ confirmedRows.length }}</strong>
      </article>
    </div>

    <div class="filter-bar">
      <label class="filter-item">
        <span>表名称</span>
        <input v-model="tableName" data-testid="own-table" placeholder="如 dwd_order_detail" />
      </label>
      <label class="filter-item">
        <span>所属库名</span>
        <input v-model="dbName" data-testid="own-db" placeholder="如 dwd_warehouse" />
      </label>
      <label class="filter-item">
        <span>提交人</span>
        <input v-model="operator" data-testid="own-operator" placeholder="签收/确认提交人" />
      </label>
      <button class="btn primary" type="button" data-testid="own-register" @click="onRegister">
        目录入库（保持库名）
      </button>
      <button class="btn" type="button" data-testid="own-reenter" @click="onReEnter">
        重新进入目录
      </button>
      <button class="btn ghost" type="button" @click="reload">刷新归属清单</button>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] || '—' }}</td>
          <td>{{ row.status }}</td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 1" class="empty-state">
            暂无归属数据，先登记一张表触发自动分层
          </td>
        </tr>
      </tbody>
    </table>

    <div class="stat-row" style="margin-top: 16px">
      <article class="stat-card">
        <span class="stat-label">签收与确认</span>
        <div class="ownership-actions">
          <select v-model="layer" data-testid="own-layer">
            <option disabled value="">选择归属分层</option>
            <option v-for="item in layers" :key="item" :value="item">{{ item }}（{{ groupOfLayer[item] }}）</option>
          </select>
          <button class="btn" type="button" data-testid="own-grant" @click="onGrant">授予确认权限</button>
          <button class="btn" type="button" data-testid="own-signoff" @click="onSignoff">提交业务签收</button>
          <button class="btn primary" type="button" data-testid="own-confirm" @click="onConfirm">确认归属并回写标签</button>
          <button class="btn ghost" type="button" data-testid="own-detail" @click="onDetail">查看来源记录</button>
        </div>
      </article>
    </div>

    <div class="stat-row" style="margin-top: 16px">
      <article class="stat-card">
        <span class="stat-label">巡检轮次派单</span>
        <div class="ownership-actions">
          <button class="btn" type="button" data-testid="own-open-round" @click="onOpenRound">开启新一轮巡检</button>
          <button class="btn" type="button" data-testid="own-dispatch-detail" @click="() => onDispatch('明细层治理组')">
            明细层治理组触发规则选择
          </button>
          <button class="btn" type="button" data-testid="own-dispatch-app" @click="() => onDispatch('应用层治理组')">
            应用层治理组触发规则选择
          </button>
          <button class="btn ghost" type="button" data-testid="own-round" @click="onRoundDetail">查看轮次领取</button>
        </div>
      </article>
    </div>

    <pre v-if="log" class="ownership-log" data-testid="own-log">{{ log }}</pre>
    <footer class="page-foot">
      <span>冲突表：{{ conflictRows.map((r) => r.表名称).join('、') || '无' }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  confirmOwnership,
  dispatchRound,
  grantPermission,
  listConflicts,
  openRound,
  ownershipDetail,
  registerCatalog,
  reEnterCatalog,
  roundDetail,
  submitSignoff,
  type OwnershipRow,
} from '@/api/ownership-service'
import { listEntries } from '@/api/backend-service'
import { moduleMeta } from '@/data/modules'

const meta = moduleMeta('tag_ownership')
const layers = ['ODS层', 'DWD层', 'DWS层', 'ADS层', 'DIM层']
const groupOfLayer: Record<string, string> = {
  ODS层: '明细层治理组',
  DWD层: '明细层治理组',
  DWS层: '应用层治理组',
  ADS层: '应用层治理组',
  DIM层: '公共维度组',
}

const rows = ref<OwnershipRow[]>([])
const conflictRows = ref<OwnershipRow[]>([])
const tableName = ref('dwd_order_detail')
const dbName = ref('dwd_warehouse')
const operator = ref('业务签收员')
const layer = ref('ADS层')
const errorMessage = ref('')
const log = ref('')

const confirmedRows = computed(() => rows.value.filter((row) => row.status === '已确认'))

function writeLog(title: string, payload: unknown) {
  log.value = `${title}\n${JSON.stringify(payload, null, 2)}`
}

async function reload() {
  errorMessage.value = ''
  try {
    const [page, conflicts] = await Promise.all([listEntries('tag_ownership', {}), listConflicts()])
    rows.value = page.items as OwnershipRow[]
    conflictRows.value = conflicts.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归属清单读取失败'
  }
}

async function guard(action: () => Promise<unknown>, title: string) {
  errorMessage.value = ''
  try {
    const payload = await action()
    writeLog(title, payload)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function onRegister() {
  return guard(() => registerCatalog(tableName.value, dbName.value), '目录入库结果')
}

function onReEnter() {
  return guard(() => reEnterCatalog(tableName.value, dbName.value), '重新进入目录结果')
}

function onGrant() {
  return guard(() => grantPermission(tableName.value, operator.value), '授权结果')
}

function onSignoff() {
  return guard(() => submitSignoff(tableName.value, layer.value, operator.value), '业务签收结果')
}

function onConfirm() {
  return guard(
    () => confirmOwnership(tableName.value, layer.value, operator.value),
    '归属确认与标签回写结果',
  )
}

function onDetail() {
  return guard(() => ownershipDetail(tableName.value), '归属来源记录')
}

function onOpenRound() {
  return guard(() => openRound(), '巡检轮次已开启')
}

function onDispatch(group: string) {
  return guard(() => dispatchRound(group), `${group}规则选择结果`)
}

function onRoundDetail() {
  return guard(() => roundDetail(), '轮次领取记录')
}

onMounted(reload)
</script>

<style scoped>
.ownership-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 8px;
}

.ownership-actions select {
  padding: 6px 10px;
  border: 1px solid var(--border-color, #d0d7de);
  border-radius: 6px;
}

.ownership-log {
  margin-top: 12px;
  padding: 12px;
  max-height: 280px;
  overflow: auto;
  background: #0f172a;
  color: #d7e3f4;
  border-radius: 8px;
  font-size: 12px;
  text-align: left;
}
</style>
