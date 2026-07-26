// Per-tool expanded views. Each component receives the tool row event
// ({input, output, ...}) and renders the input side of the card; the shared
// output block is rendered by the caller. Tools not listed here fall back to
// the generic key/value dump in EventRow.

import { diffLines } from '../diff'

export const TOOL_DETAIL = {
  Bash: BashDetail,
  Edit: EditDetail,
  Write: WriteDetail,
  Read: ReadDetail,
  TodoWrite: TodoDetail,
  WebSearch: WebDetail,
  WebFetch: WebDetail,
}

function BashDetail({ ev }) {
  const { command, description } = ev.input ?? {}
  return (
    <div className="term">
      {description && <div className="term-desc">{description}</div>}
      <div className="term-cmd"><span className="prompt">$</span>{command ?? ''}</div>
    </div>
  )
}

// Line-level diff between old_string and new_string; falls back to the plain
// two-block view when the strings are too large to diff comfortably.
function EditDetail({ ev }) {
  const { file_path, old_string = '', new_string = '', replace_all } = ev.input ?? {}
  const diff = diffLines(old_string, new_string)
  return (
    <div>
      <div className="diff-file">{file_path}{replace_all ? ' · replace all' : ''}</div>
      {diff ? (
        <div className="diff">
          {diff.map((d, i) => (
            <div key={i} className={`dl ${d.t}`}>
              <span className="dl-sign">{d.t === 'add' ? '+' : d.t === 'del' ? '-' : ' '}</span>
              {d.s}
            </div>
          ))}
        </div>
      ) : (
        <>
          <div className="diff-block old">{old_string}</div>
          <div className="diff-block new">{new_string}</div>
        </>
      )}
    </div>
  )
}

function WriteDetail({ ev }) {
  const { file_path, content = '' } = ev.input ?? {}
  return (
    <div>
      <div className="diff-file">{file_path}</div>
      <div className="diff-block new">{content}</div>
    </div>
  )
}

function ReadDetail({ ev }) {
  const { file_path, offset, limit } = ev.input ?? {}
  const range = offset != null || limit != null
    ? ` · lines ${offset ?? 1}–${offset != null && limit != null ? offset + limit : limit ?? 'end'}`
    : ''
  return <div className="diff-file">{file_path}{range}</div>
}

const TODO_MARK = { completed: '[x]', in_progress: '[~]', pending: '[ ]' }

function TodoDetail({ ev }) {
  const todos = ev.input?.todos ?? []
  return (
    <div className="todo-list">
      {todos.map((t, i) => (
        <div key={i} className={`todo ${t.status ?? 'pending'}`}>
          <span className="todo-mark">{TODO_MARK[t.status] ?? '[ ]'}</span>
          {t.subject ?? t.content ?? ''}
        </div>
      ))}
    </div>
  )
}

function WebDetail({ ev }) {
  const { query, url, prompt } = ev.input ?? {}
  return (
    <div>
      {query && <div className="kv"><span className="k">query</span><span className="v">{query}</span></div>}
      {url && (
        <div className="kv">
          <span className="k">url</span>
          <a className="v" href={url} target="_blank" rel="noopener noreferrer">{url}</a>
        </div>
      )}
      {prompt && <div className="kv"><span className="k">prompt</span><span className="v">{prompt}</span></div>}
    </div>
  )
}
