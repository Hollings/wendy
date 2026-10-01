import { useState } from 'react'
import { agoShort, contextWindowFor, formatTokens, shortModel } from '../events'
export default function SessionsPanel({ channelsMap, channelStats, events, source, onSelect }) {
  const [showAll, setShowAll] = useState(false)
  const loaded = new Set(events.filter(event => event.channel_id).map(event => String(event.channel_id)))
  const selectedId = source.startsWith('channel:') ? source.slice(8) : null
  const ids = [...new Set([...Object.keys(channelsMap), ...Object.keys(channelStats), ...loaded, ...(selectedId ? [selectedId] : [])])].sort((a, b) => (channelsMap[a] || a).localeCompare(channelsMap[b] || b) || a.localeCompare(b))
  const visibleIds = showAll ? ids : ids.filter(id => loaded.has(id) || id === selectedId)
  const nameCounts = new Map()
  for (const id of ids) {
    const name = (channelsMap[id] || id).trim().toLowerCase()
    nameCounts.set(name, (nameCounts.get(name) || 0) + 1)
  }
  const hasUnloaded = ids.some(id => !loaded.has(id) && id !== selectedId)
  return <nav className="source-navigation" aria-label="Channels">
    <div className="nav-section-label">CHANNELS<span>{visibleIds.length}</span></div>
    <button className={'source-button all-sources' + (source === 'all' ? ' selected' : '')} onClick={() => onSelect('all')} aria-pressed={source === 'all'}><span>*</span>All sources</button>
    <div className="channel-list">
      {visibleIds.length === 0 && <p className="nav-empty">No channel events loaded</p>}
      {visibleIds.map(id => {
        const stats = channelStats[id], known = stats?.tokens != null
        const percent = known ? Math.min(100, stats.tokens / contextWindowFor(stats.model) * 100) : null
        const name = channelsMap[id] || id
        const label = nameCounts.get(name.trim().toLowerCase()) > 1 ? name + ' · …' + id.slice(-6) : name
        return <button className={'source-button channel-button' + (source === 'channel:' + id ? ' selected' : '')} key={id} onClick={() => onSelect('channel:' + id)} aria-pressed={source === 'channel:' + id} title={'channel: ' + id + (stats?.model ? '\nmodel: ' + stats.model : '')}>
          <span className="channel-name"><span className="hash">#</span><span>{label}</span><span className="channel-age">{stats?.lastTs ? agoShort(stats.lastTs) : '—'}</span></span>
          {stats && <span className="channel-caption"><span>{stats.model ? shortModel(stats.model) : 'unknown model'}</span><span>{known ? formatTokens(stats.tokens) + ' / ' + Math.round(percent) + '%' : 'ctx —'}</span></span>}
          {known && <span className={'context-track' + (percent > 85 ? ' context-high' : '')} role="meter" aria-label={(channelsMap[id] || id) + ' context usage'} aria-valuenow={Math.round(percent)} aria-valuemin={0} aria-valuemax={100} title={Math.round(percent) + '% of estimated context window'}><span style={{ width: percent + '%' }} /></span>}
        </button>
      })}
    </div>
    {hasUnloaded && <button className="nav-all-tasks" onClick={() => setShowAll(value => !value)} aria-expanded={showAll} title="Channels without events in the loaded buffer remain available here.">{showAll ? 'Hide channels without events' : 'Show all channels (' + ids.length + ')'}</button>}
  </nav>
}
