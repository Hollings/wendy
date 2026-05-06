import WendyGreeble from '../WendyGreeble'

const MOOD_WORDS = { idle: 'resting', thinking: 'thinking', tooling: 'running a tool', warn: 'near context cap' }
const MOOD_SUB = {
  idle: 'no active tool calls · breathing ~16 bpm',
  thinking: 'internal monologue · waveform rising',
  tooling: 'scan bar locked to active tool stream',
  warn: 'one session is close to 200k · consider a new bead',
}

function formatTokens(n) {
  if (!n) return '0'
  if (n < 1000) return String(n)
  return (n / 1000).toFixed(1) + 'k'
}

export default function GreeblePanel({ mood, tokens, turns, activeTasks, channelHue }) {
  return (
    <div className="panel greeble-panel">
      <div className="greeble-face">
        <div className="plate">
          <span className="screw tl" /><span className="screw tr" />
          <span className="screw bl" /><span className="screw br" />
          <WendyGreeble mood={mood} channelHue={channelHue} />
        </div>
        <div className="greeble-status-block">
          <div className="greeble-status">wendy is <em>{MOOD_WORDS[mood]}</em></div>
          <div className="greeble-sub">{MOOD_SUB[mood]}</div>
        </div>
      </div>
      <div className="greeble-readout">
        <div className="cell"><div className="k">ctx</div><div className="v accent mono">{formatTokens(tokens)}</div></div>
        <div className="cell"><div className="k">tasks</div><div className="v mono">{activeTasks}</div></div>
        <div className="cell"><div className="k">turns</div><div className="v mono">{turns}</div></div>
      </div>
    </div>
  )
}
