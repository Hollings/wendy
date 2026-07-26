// Render-time feed derivation. Pure function of the parsed event list:
//
//   1. Pair tool_result rows with their tool_use row (by toolUseId) so a call
//      and its output render as one card, with duration.
//   2. Assign every row to a turn (the span between system/init and result
//      for one source) and build per-turn summaries.
//   3. Drop kinds the user has filtered out -- after pairing and turn
//      accounting so outputs still attach and summaries stay truthful.
//   4. Collapse consecutive identical tool calls from the same source into a
//      single xN row (check_messages polls, retry loops).
//   5. Insert quiet-gap markers between turns.
//
// Runs on every render over at most MAX_EVENTS (300) rows -- cheap enough
// that no memoization beyond the caller's useMemo is needed.

import { toolPreview, parseSentResult, parseMsgsOutput } from './events.js'

// A silence this long between turns earns a visible gap marker.
const GAP_MS = 20 * 60 * 1000

// pendingByToolId sentinel: the call row was dropped by the kind filter.
const HIDDEN_CALL = -1

const sourceKey = ev => `${ev.channel_id ?? ''}|${ev.bead_id ?? ''}`
const toolSig = ev => `${ev.tool}|${ev.flavor ?? ''}|${toolPreview(ev.tool, ev.input)}`

/**
 * @returns {{rows: object[], turns: Map<string, object>, lastTurnBySource: Map<string, string>}}
 *   rows  display rows in feed order (includes kind:'gap' pseudo-rows)
 *   turns turnId -> {id, src, startEv, endEv, closed, counts, errors}
 */
export function deriveFeed(events, hiddenKindSet) {
  const rows = []
  const turns = new Map()
  const pendingByToolId = new Map() // toolUseId -> index into rows
  const activeTurn = new Map()      // sourceKey -> turnId
  const lastTurnBySource = new Map()

  for (const ev of events) {
    const src = sourceKey(ev)

    // -- 1. absorb tool results into their originating call ------------------
    // HIDDEN_CALL marks a call the kind filter dropped: its result is part of
    // the same card and disappears with it rather than surfacing as an orphan.
    if (ev.kind === 'result' && ev.toolUseId != null && pendingByToolId.has(ev.toolUseId)) {
      const idx = pendingByToolId.get(ev.toolUseId)
      pendingByToolId.delete(ev.toolUseId)
      if (idx !== HIDDEN_CALL) {
        rows[idx] = attachOutput(rows[idx], ev)
        const turn = turns.get(rows[idx].turnId)
        if (turn && ev.isError) turn.errors++
      }
      continue
    }

    // -- 2. turn assignment ---------------------------------------------------
    let turnId = activeTurn.get(src) ?? null
    if (ev.kind === 'session_start') {
      turnId = ev.id
      activeTurn.set(src, turnId)
      lastTurnBySource.set(src, turnId)
      turns.set(turnId, {
        id: turnId, src, startEv: ev, endEv: null, closed: false,
        counts: { events: 0, said: 0, thoughts: 0, tools: 0 }, errors: 0,
      })
    }
    const row = { ...ev, turnId }
    const turn = turnId ? turns.get(turnId) : null

    if (turn && ev.kind === 'session_end') {
      turn.endEv = row
      turn.closed = true
      activeTurn.delete(src)
    } else if (turn && ev.kind !== 'session_start') {
      turn.counts.events++
      if (ev.kind === 'discord' && ev.sub !== 'reaction') turn.counts.said++
      if (ev.kind === 'thinking') turn.counts.thoughts++
      if (ev.kind === 'tool') turn.counts.tools++
      if (ev.kind === 'result' && ev.isError) turn.errors++
    }

    // -- 3. kind filter -------------------------------------------------------
    if (hiddenKindSet?.has(ev.kind)) {
      if ((ev.kind === 'tool' || ev.kind === 'discord') && ev.toolUseId != null) {
        pendingByToolId.set(ev.toolUseId, HIDDEN_CALL)
      }
      continue
    }

    // -- 4. collapse repeated identical tool calls -----------------------------
    // A poll loop reads as one xN row instead of N. The previous identical
    // call may be separated from this one by thinking/rate-limit rows (they
    // interleave constantly), so scan back over those. The old row is nulled
    // in place -- positions must stay stable for pendingByToolId -- and the
    // merged row lands at the current (chronologically correct) position.
    if (row.kind === 'tool') {
      const k = findStackTarget(rows, row, src, pendingByToolId)
      if (k !== -1) {
        const prev = rows[k]
        rows[k] = null
        if (row.toolUseId != null) pendingByToolId.set(row.toolUseId, rows.length)
        rows.push({ ...row, stackCount: (prev.stackCount ?? 1) + 1, firstTs: prev.firstTs ?? prev.ts })
        continue
      }
    }

    if ((row.kind === 'tool' || row.kind === 'discord') && row.toolUseId != null) {
      pendingByToolId.set(row.toolUseId, rows.length)
    }
    rows.push(row)
  }

  // Calls still waiting on output: pulse only while their turn is open. A
  // closed turn means the output never made it into the stream -- show quiet.
  for (const idx of pendingByToolId.values()) {
    if (idx === HIDDEN_CALL) continue
    const row = rows[idx]
    const turn = row.turnId ? turns.get(row.turnId) : null
    rows[idx] = { ...row, awaiting: !turn || !turn.closed }
  }

  // -- 5. quiet-gap markers between turns -------------------------------------
  const withGaps = []
  for (const row of rows) {
    if (row === null) continue // stack-merged away
    const prev = withGaps[withGaps.length - 1]
    if (prev && row.ts - prev.ts > GAP_MS && prev.turnId !== row.turnId) {
      withGaps.push({ id: `gap-${row.id}`, kind: 'gap', ts: row.ts, ms: row.ts - prev.ts, turnId: null })
    }
    withGaps.push(row)
  }

  return { rows: withGaps, turns, lastTurnBySource }
}

// Rows a stack merge may scan past: high-frequency interleave that sits
// between two polls of the same command without making them "different".
const STACK_SKIP = new Set(['thinking', 'rate_limit'])
const STACK_SCAN_LIMIT = 100 // total rows examined, bounds the work only

/**
 * Index of an earlier identical tool call this row can fold into, or -1.
 * Walks back over same-source thinking/rate-limit rows and any number of
 * other-source rows; stops at the first substantive same-source row.
 */
function findStackTarget(rows, row, src, pendingByToolId) {
  const floor = Math.max(0, rows.length - STACK_SCAN_LIMIT)
  for (let k = rows.length - 1; k >= floor; k--) {
    const cand = rows[k]
    if (cand === null) continue
    if (sourceKey(cand) !== src) continue // other sources interleave freely
    if (STACK_SKIP.has(cand.kind)) continue
    if (
      cand.kind === 'tool' && cand.turnId === row.turnId &&
      toolSig(cand) === toolSig(row) &&
      !(cand.toolUseId != null && pendingByToolId.has(cand.toolUseId)) &&
      stackable(cand)
    ) return k
    return -1 // a substantive row separates the calls -- keep both visible
  }
  return -1
}

// Only fold away calls whose outcome carried no information: a failed call or
// a msgs poll that actually returned messages must stay its own row, because
// the stack shows the LATEST output only.
function stackable(prev) {
  if (prev.output?.isError) return false
  if (prev.flavor === 'msgs') {
    const entries = parseMsgsOutput(prev.output?.content ?? '')
    return entries != null && entries.length === 0
  }
  return true
}

// Fold a tool_result row into its call row. For discord sends the result
// echoes the delivered text -- that is the ground truth, so it replaces
// whatever the command parser guessed.
function attachOutput(row, res) {
  const output = { content: res.content ?? '', isError: !!res.isError, ts: res.ts }
  const next = { ...row, output, durationMs: Math.max(0, res.ts - row.ts), awaiting: false }
  if (next.kind === 'discord' && next.sub !== 'reaction') {
    const delivered = parseSentResult(output.content)
    if (delivered != null) next.text = delivered || next.text
    if (output.isError) next.blocked = true
  }
  return next
}
