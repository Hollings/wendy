import { useEffect, useRef } from 'react'

// Wendy's "face" — a 56x56 instrument-panel readout that changes speed,
// intensity, and hue based on mood. No eyes, no mouth; just behavior.
// Moods: 'idle' | 'thinking' | 'tooling' | 'warn'

export default function WendyGreeble({ mood = 'thinking', channelHue = 145 }) {
  const canvasRef = useRef(null)
  const rafRef = useRef(0)
  const tRef = useRef(0)

  useEffect(() => {
    const c = canvasRef.current
    if (!c) return
    const ctx = c.getContext('2d')
    const DPR = window.devicePixelRatio || 1
    const W = 56, H = 56
    c.width = W * DPR; c.height = H * DPR
    c.style.width = W + 'px'; c.style.height = H + 'px'
    ctx.scale(DPR, DPR)

    const speedByMood = { idle: 0.3, thinking: 1.0, tooling: 1.6, warn: 1.1 }
    const hueByMood = { idle: channelHue, thinking: channelHue, tooling: channelHue, warn: 78 }

    const draw = () => {
      const speed = speedByMood[mood] ?? 1
      tRef.current += 0.016 * speed
      const t = tRef.current
      const hue = hueByMood[mood] ?? channelHue
      const accent = `oklch(0.82 0.15 ${hue})`
      const accentDim = `oklch(0.52 0.12 ${hue})`
      const grid = `oklch(0.30 0.01 60)`

      ctx.clearRect(0, 0, W, H)

      ctx.strokeStyle = grid
      ctx.lineWidth = 0.5
      for (let x = 0; x <= W; x += 7) {
        ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, H); ctx.stroke()
      }
      for (let y = 0; y <= H; y += 7) {
        ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(W, y + 0.5); ctx.stroke()
      }

      // Top: waveform
      ctx.strokeStyle = accent
      ctx.lineWidth = 1.2
      ctx.beginPath()
      for (let x = 2; x < W - 2; x++) {
        const nx = x / (W - 4)
        const amp = mood === 'idle' ? 1.5 : mood === 'thinking' ? 5 : mood === 'warn' ? 6 : 8
        const y =
          12 +
          Math.sin(nx * 8 + t * 3) * amp * 0.6 +
          Math.sin(nx * 3 + t * 1.4) * amp * 0.4 +
          (mood === 'tooling' ? Math.sin(nx * 20 + t * 5) * 1.2 : 0)
        if (x === 2) ctx.moveTo(x, y); else ctx.lineTo(x, y)
      }
      ctx.stroke()

      // Middle: scan bar
      const scanProg = (t * 0.6) % 1
      const scanX = 2 + scanProg * (W - 4)
      const grad = ctx.createLinearGradient(scanX - 12, 0, scanX + 12, 0)
      grad.addColorStop(0, 'rgba(0,0,0,0)')
      grad.addColorStop(0.5, accentDim)
      grad.addColorStop(1, 'rgba(0,0,0,0)')
      ctx.fillStyle = grad
      ctx.globalAlpha = mood === 'idle' ? 0.15 : mood === 'warn' ? 0.35 : 0.55
      ctx.fillRect(2, 22, W - 4, 12)
      ctx.globalAlpha = 1
      ctx.strokeStyle = grid
      ctx.strokeRect(2, 22, W - 4, 12)
      for (let i = 0; i < 8; i++) {
        const x = 4 + i * ((W - 8) / 7)
        const pulse = (Math.sin(t * 3 + i) + 1) / 2
        const near = 1 - Math.min(1, Math.abs(x - scanX) / 14)
        ctx.fillStyle = accent
        ctx.globalAlpha = 0.2 + near * 0.7 * (mood === 'idle' ? 0.3 : 1) + pulse * 0.1
        ctx.beginPath(); ctx.arc(x, 28, 1.4, 0, Math.PI * 2); ctx.fill()
      }
      ctx.globalAlpha = 1

      // Bottom: rotating gear
      ctx.save()
      ctx.translate(13, 46)
      const gspeed = mood === 'tooling' ? 4 : mood === 'thinking' ? 1.2 : 0.4
      ctx.rotate(t * gspeed)
      ctx.strokeStyle = accent
      ctx.lineWidth = 1.1
      const teeth = 8, rOuter = 6, rInner = 4
      ctx.beginPath()
      for (let i = 0; i < teeth * 2; i++) {
        const a = (i / (teeth * 2)) * Math.PI * 2
        const r = i % 2 === 0 ? rOuter : rInner
        const px = Math.cos(a) * r, py = Math.sin(a) * r
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py)
      }
      ctx.closePath()
      ctx.stroke()
      ctx.fillStyle = accentDim
      ctx.beginPath(); ctx.arc(0, 0, 1.8, 0, Math.PI * 2); ctx.fill()
      ctx.restore()

      // Heartbeat dot
      const beat = mood === 'idle'
        ? 0.5 + 0.5 * Math.sin(t * 1.2)
        : 0.6 + 0.4 * Math.sin(t * (mood === 'warn' ? 6 : 3))
      ctx.fillStyle = accent
      ctx.globalAlpha = 0.3 + beat * 0.7
      ctx.beginPath(); ctx.arc(28, 46, 2 + beat * 1.3, 0, Math.PI * 2); ctx.fill()
      ctx.globalAlpha = 1

      // Vertical tick readout
      ctx.strokeStyle = accentDim
      ctx.lineWidth = 1
      for (let i = 0; i < 5; i++) {
        const h = 1.5 + ((Math.sin(t * 2 + i * 1.3) + 1) / 2) * 6
        ctx.globalAlpha = 0.35 + (i === Math.floor(t * 2) % 5 ? 0.5 : 0)
        ctx.beginPath()
        ctx.moveTo(38 + i * 3, 50)
        ctx.lineTo(38 + i * 3, 50 - h)
        ctx.stroke()
      }
      ctx.globalAlpha = 1

      rafRef.current = requestAnimationFrame(draw)
    }

    rafRef.current = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(rafRef.current)
  }, [mood, channelHue])

  return <canvas ref={canvasRef} />
}
