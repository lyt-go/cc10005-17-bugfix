import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/captcha'
const columns = [
  "策略编号",
  "验证码类型",
  "识别服务商",
  "识别超时秒",
  "重试次数上限",
  "降级策略",
  "手动打码开关",
  "打码等待队列"
]
const actions = [
  "提交策略验证",
  "启用策略",
  "降级策略",
  "完成人工打码",
  "领取人工打码名额"
]
const statuses = [
  "草稿",
  "策略验证中",
  "已启用",
  "已降级"
]
const stats = [
  {
    "label": "验证码策略总数",
    "value": 0
  },
  {
    "label": "已启用策略",
    "value": 0
  },
  {
    "label": "验证中策略",
    "value": 0
  }
]
// 统计口径直接来自列表当前结果，动作后随列表一起刷新，不会停留在旧数字上。
function buildStats(rows: Row[]) {
  return stats.map((item) => {
    if (item.label === "已启用策略") {
      return { ...item, value: rows.filter((row) => row["状态"] === "已启用").length }
    }
    if (item.label === "验证中策略") {
      return { ...item, value: rows.filter((row) => row["状态"] === "策略验证中").length }
    }
    return { ...item, value: rows.length }
  })
}
const detailFields = [
  "状态",
  "策略编号",
  "验证码类型",
  "识别服务商",
  "识别超时秒",
  "重试次数上限",
  "降级策略",
  "手动打码开关",
  "打码等待队列",
  "冻结服务商",
  "冻结超时上限",
  "降级结论",
  "状态说明"
]

type Row = Record<string, string | number | null | unknown>

export default function CaptchaView() {
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
        throw new Error('验证码策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setRows([])
      setTotal(0)
      setErrorMessage(error instanceof Error ? error.message : '验证码处理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const reloadDetail = useCallback(async (entryId: number) => {
    try {
      const response = await request(`${ENDPOINT}/${entryId}`)
      if (!response.ok) {
        throw new Error('验证码策略详情读取失败')
      }
      setDetail(await response.json())
    } catch (error) {
      setDetail(null)
      setErrorMessage(error instanceof Error ? error.message : '验证码策略详情读取失败')
    }
  }, [])

  // 列表和详情都来自后端同一份取数结果；动作成功后两边一起刷新，避免状态残留。
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
        throw new Error(payload.message || '验证码处理动作未生效，请稍后重试')
      }
      setDetail(payload.entry ?? null)
      setSelectedId(entryId)
      await syncEntries(entryId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '验证码处理操作失败')
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
    <section className="page" data-module="captcha">
      <header className="page-head">
        <div>
          <h2>验证码处理管理</h2>
          <p className="page-desc">管理各类型验证码的自动识别与人工打码策略，包括图像验证码、滑块和点击验证。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('验证码策略登记入口尚未接入审批流')}>
            登记验证码策略
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出验证码处理清单
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
              <td className="empty-state" colSpan={columns.length + 2}>暂无验证码处理数据，可先登记验证码策略</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <aside className="detail-panel">
          <header className="detail-head">
            <h3>策略详情 · {String(detail['策略编号'] ?? '')}</h3>
            <button className="btn ghost" type="button" onClick={() => { setDetail(null); setSelectedId(null) }}>收起</button>
          </header>
          <dl className="detail-list">
            {detailFields.map((field) => (
              <div key={field} className="detail-item">
                <dt>{field}</dt>
                <dd>{String(detail[field] ?? '—') || '—'}</dd>
              </div>
            ))}
            <div className="detail-item">
              <dt>关联渲染配置</dt>
              <dd>
                {Array.isArray(detail['关联渲染配置']) && (detail['关联渲染配置'] as Row[]).length > 0
                  ? (detail['关联渲染配置'] as Row[]).map((item) => (
                    `${item['配置编号']}（${item['status']}）`
                  )).join('、')
                  : '—'}
              </dd>
            </div>
            <div className="detail-item">
              <dt>降级历史</dt>
              <dd>
                {Array.isArray(detail['降级历史']) && (detail['降级历史'] as Row[]).length > 0
                  ? (detail['降级历史'] as Row[]).map((item) => (
                    `第${item['序号']}次 · ${item['冻结服务商']} / ${item['冻结超时上限']}秒 · ${item['时间']}`
                  )).join('；')
                  : '—'}
              </dd>
            </div>
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
        <span>共 {total} 条验证码处理记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
