// Compatibility export for existing replay scripts. The viewer no longer
// folds turns or stacks distinct calls; all details stay explicitly available.
import { buildTimeline } from './timeline.js'
export function deriveFeed(events, hiddenKinds = new Set()) {
  return { rows: buildTimeline(events).filter(event => !hiddenKinds.has(event.kind)), turns: new Map(), lastTurnBySource: new Map() }
}
