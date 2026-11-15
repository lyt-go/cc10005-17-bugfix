import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/jsrender'
const columns = [
  "配置编号",
  "目标URL",
  "浏览器类型",
  "渲染等待秒",
  "滚动次数",
  "点击选择器",
  "页面等待条件",
  "截屏开关"
]
const actions = [
  "提交渲染测试",
  "标记上线",
  "标记降级",
  "领取人工打码名额"
]
const statuses = [
  "草稿",
  "渲染测试中",
  "已上线",
  "已降级"
]
const stats = [
  {
    "label": "渲染配置总数",
    "value": 0
  },
  {
    "label": "已上线配置",
    "value": 0
  },
  {
    "label": "测试中配置",
    "value": 0
  }
]
// 统计口径直接来自列表当前结果，动作后随列表一起刷新，避免留下旧状态。
function buildStats(rows: Row[]) {
  return stats.map((item) => {
    if (item.label === "已上线配置") {
      return { ...item, value: rows.filter((row) => row["状态"] === "已上线").length }
    }
    if (item.label === "测试中配置") {
      return { ...item, value: rows.filter((row) => row["状态"] === "渲染测试中").length }
    }
    return { ...item, value: rows.length }
  })
}
const detailFields = [
  "状态",
  "配置编号",
  "目标URL",
  "浏览器类型",
  "关联策略编号",
  "关联策略状态",
  "关联策略冻结服务商",
  "关联策略冻结超时上限",
  "采集结果",
  "冻结采集结果",
  "降级说明",
  "状态说明"
]

type Row = Record<string, string | number | null | unknown>

export default function JsrenderView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState({ keyword: '', status: '' })
  const [detail, setDetail] = useState<Row | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 1), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (filters.keyword) query.set('keyword', filters.keyword)
    if (filters.status) query.set('status', filters.status)
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('渲染配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setRows([])
      setTotal(0)
      setErrorMessage(error instanceof Error ? error.message : 'JS渲染采集列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const reloadDetail = useCallback(async (entryId: number) => {
    try {
      const response = await request(`${ENDPOINT}/${entryId}`)
      if (!response.ok) {
        throw new Error('渲染配置详情读取失败')
      }
      setDetail(await response.json())
    } catch (error) {
      setDetail(null)
      setErrorMessage(error instanceof Error ? error.message : '渲染配置详情读取失败')
    }
  }, [])

  // 列表与详情读取的是后端同一份记录；动作完成后两边一起刷新，刷新后也不会留下旧结论。
  const syncEntries = useCallback(async (entryId: number) => {
    await Promise.all([reload(), reloadDetail(entryId)])
  }, [reload, reloadDetail])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    const entryId = Number(row.id)
    try {
      const response = await request(`${ENDPOINT}/${entryId}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || 'JS渲染采集动作未生效，请稍后重试')
      }
      setDetail(payload.entry ?? null)
      setSelectedId(entryId)
      await syncEntries(entryId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'JS渲染采集操作失败')
      await syncEntries(entryId)
    }
  }

  function openDetail(row: Row) {
    setSelectedId(Number(row.id))
    void reloadDetail(Number(row.id))
  }

  function allowedActions(row: Row): string[] {
    const allowed = row.allowed_actions
    return Array.isArray(allowed) ? allowed.map(String) : []
  }

  return (
    <section className="page" data-module="jsrender">
      <header className="page-head">
        <div>
          <h2>JS渲染采集管理</h2>
          <p className="page-desc">管理需要JavaScript渲染的动态页面采集配置，定义浏览器环境、渲染等待和交互操作。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('渲染配置登记入口尚未接入审批流')}>
            登记渲染配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出JS渲染采集清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {buildStats(rows).map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters.keyword}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, keyword: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({ keyword: '', status: '' })}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>状态</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>关联策略</th>
            <th>采集结果</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={String(row.id)}
              className={selectedId === Number(row.id) ? 'selected-row' : undefined}
              onClick={() => openDetail(row)}
            >
              <td>{String(row['状态'] ?? '—')}</td>
              {columns.map((column) => <td key={column}>{String(row[column] ?? '—')}</td>)}
              <td>{String(row['关联策略编号'] ?? '—')}（{String(row['关联策略状态'] ?? '—')}）</td>
              <td>{String(row['采集结果'] ?? '—')}</td>
              <td className="row-actions" onClick={(event) => event.stopPropagation()}>
                {allowedActions(row).length === 0 && <span className="muted-text">只读</span>}
                {actions.filter((action) => allowedActions(row).includes(action)).map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无JS渲染采集数据，可先登记渲染配置</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <aside className="detail-panel">
          <header className="detail-head">
            <h3>渲染配置详情 · {String(detail['配置编号'] ?? '')}</h3>
            <button className="btn ghost" type="button" onClick={() => { setDetail(null); setSelectedId(null) }}>收起</button>
          </header>
          <dl className="detail-list">
            {detailFields.map((field) => (
              <div key={field} className="detail-item">
                <dt>{field}</dt>
                <dd>{String(detail[field] ?? '—') || '—'}</dd>
              </div>
            ))}
          </dl>
          <div className="row-actions">
            {actions.filter((action) => allowedActions(detail).includes(action)).map((action) => (
              <button key={action} className="link" type="button" onClick={() => void runAction(action, detail)}>
                {action}
              </button>
            ))}
          </div>
        </aside>
      )}

      <footer className="page-foot">
        <span>共 {total} 条JS渲染采集记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
