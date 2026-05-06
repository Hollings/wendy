// Stable per-channel color derived from channel_id.
// Picks a hue from a small curated palette so channel chips stay visually
// distinguishable even with many channels.

const HUES = [145, 220, 78, 305, 18, 175, 260, 40, 330, 200]

function hashString(s) {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return Math.abs(h)
}

export function channelHue(channelId) {
  if (!channelId) return 145
  return HUES[hashString(String(channelId)) % HUES.length]
}

export function channelColor(channelId) {
  const h = channelHue(channelId)
  return `oklch(0.78 0.15 ${h})`
}
