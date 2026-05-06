import GreeblePanel from './panels/GreeblePanel'
import ContextPanel from './panels/ContextPanel'
import ChannelsPanel from './panels/ChannelsPanel'
import BeadsPanel from './panels/BeadsPanel'

export default function Sidebar({
  mood, channelHue, totalTokens, turns, activeTasks,
  sessions,
  channels, channelActivity, hiddenChannels, onToggleChannel,
  beads, beadSnippets, focusedBeadId, onBeadClick,
}) {
  return (
    <aside className="sidebar">
      <GreeblePanel
        mood={mood}
        channelHue={channelHue}
        tokens={totalTokens}
        turns={turns}
        activeTasks={activeTasks}
      />
      <ContextPanel sessions={sessions} />
      <ChannelsPanel
        channels={channels}
        activity={channelActivity}
        hiddenChannels={hiddenChannels}
        onToggle={onToggleChannel}
      />
      <BeadsPanel
        beads={beads}
        beadSnippets={beadSnippets}
        focusedBeadId={focusedBeadId}
        onBeadClick={onBeadClick}
      />
    </aside>
  )
}
