// Minimal markdown for prose bodies (thoughts, speech, discord sends).
// Handles what the model actually writes by habit: fenced code, inline code,
// bold, italic, links, bare URLs, and lists. Everything is emitted as React
// elements -- no HTML injection surface. Not handled on purpose: headings,
// tables, images, blockquotes (rare in this stream, and headings would let a
// chat message shout over the dashboard chrome).

const INLINE = new RegExp(
  [
    '(`[^`\\n]+`)',                          // 1 inline code
    '(\\*\\*[^*\\n]+\\*\\*)',                // 2 bold
    '(\\*[^*\\s][^*\\n]*\\*)',               // 3 italic
    '(\\[([^\\]\\n]+)\\]\\((https?://[^\\s)]+)\\))', // 4(5,6) link
    '(https?://[^\\s<>()"]+)',               // 7 bare URL
  ].join('|'),
  'g',
)

function renderInline(text, keyBase) {
  const out = []
  let last = 0
  let k = 0
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const key = `${keyBase}-${k++}`
    if (m[1]) out.push(<code key={key}>{m[1].slice(1, -1)}</code>)
    else if (m[2]) out.push(<strong key={key}>{renderInline(m[2].slice(2, -2), key)}</strong>)
    else if (m[3]) out.push(<em key={key}>{renderInline(m[3].slice(1, -1), key)}</em>)
    else if (m[4]) out.push(<a key={key} href={m[6]} target="_blank" rel="noopener noreferrer">{m[5]}</a>)
    else if (m[7]) out.push(<a key={key} href={m[7]} target="_blank" rel="noopener noreferrer">{m[7]}</a>)
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const LIST_ITEM = /^(\s*)(?:[-*]|\d+\.)\s+(.*)$/

// Render a fence-free segment: alternating paragraphs and lists.
function renderProse(text, keyBase) {
  const out = []
  const lines = text.split('\n')
  let para = []
  let list = null // {ordered, items}

  const flushPara = () => {
    if (!para.length) return
    out.push(<p key={`${keyBase}-p${out.length}`}>{renderInline(para.join('\n'), `${keyBase}-p${out.length}`)}</p>)
    para = []
  }
  const flushList = () => {
    if (!list) return
    const Tag = list.ordered ? 'ol' : 'ul'
    out.push(
      <Tag key={`${keyBase}-l${out.length}`}>
        {list.items.map((item, i) => <li key={i}>{renderInline(item, `${keyBase}-l${out.length}-${i}`)}</li>)}
      </Tag>,
    )
    list = null
  }

  for (const line of lines) {
    const li = LIST_ITEM.exec(line)
    if (li) {
      flushPara()
      const ordered = /^\s*\d+\./.test(line)
      if (!list || list.ordered !== ordered) { flushList(); list = { ordered, items: [] } }
      list.items.push(li[2])
    } else if (line.trim() === '') {
      flushPara()
      flushList()
    } else {
      flushList()
      para.push(line)
    }
  }
  flushPara()
  flushList()
  return out
}

export default function Markdown({ text }) {
  const src = text ?? ''
  // Fences first: segments alternate prose / code. An unbalanced trailing
  // fence (mid-stream truncation) just renders its tail as code -- fine.
  const segs = src.split('```')
  const out = []
  for (let i = 0; i < segs.length; i++) {
    if (i % 2 === 0) {
      if (segs[i]) out.push(...renderProse(segs[i], `s${i}`))
    } else {
      const code = segs[i].replace(/^[\w-]*\n/, '').replace(/\n$/, '')
      out.push(<pre key={`c${i}`} className="md-code">{code}</pre>)
    }
  }
  return <div className="md">{out}</div>
}
