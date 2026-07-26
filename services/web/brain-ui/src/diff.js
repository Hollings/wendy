// Small line-level LCS diff for Edit tool cards.

const MAX_CELLS = 40_000 // ~200x200 lines; beyond this the DP table isn't worth it

/**
 * Diff two strings line-by-line.
 * @returns {Array<{t: 'ctx'|'del'|'add', s: string}>|null} null when the
 *   inputs are too large -- caller falls back to plain old/new blocks.
 */
export function diffLines(a, b) {
  const A = String(a).split('\n')
  const B = String(b).split('\n')
  const m = A.length
  const n = B.length
  if (m * n > MAX_CELLS) return null

  // LCS lengths, computed bottom-up.
  const dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1))
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }

  const out = []
  let i = 0
  let j = 0
  while (i < m && j < n) {
    if (A[i] === B[j]) { out.push({ t: 'ctx', s: A[i] }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: 'del', s: A[i] }); i++ }
    else { out.push({ t: 'add', s: B[j] }); j++ }
  }
  while (i < m) out.push({ t: 'del', s: A[i++] })
  while (j < n) out.push({ t: 'add', s: B[j++] })

  return squeezeContext(out)
}

// Long unchanged runs collapse to a single elision marker so the changed
// lines stay in view.
function squeezeContext(diff, keep = 2) {
  const out = []
  let run = []
  const flush = (isEdge) => {
    if (run.length <= keep * 2 + 1) {
      out.push(...run)
    } else {
      // Keep the tail of a leading run / head of a trailing run.
      out.push(...run.slice(0, isEdge === 'lead' ? 0 : keep))
      out.push({ t: 'skip', s: `··· ${run.length - (isEdge ? keep : keep * 2)} unchanged lines` })
      out.push(...run.slice(isEdge === 'trail' ? run.length : run.length - keep))
    }
    run = []
  }
  for (let idx = 0; idx < diff.length; idx++) {
    const d = diff[idx]
    if (d.t === 'ctx') { run.push(d); continue }
    flush(out.length === 0 ? 'lead' : null)
    out.push(d)
  }
  flush('trail')
  return out
}
