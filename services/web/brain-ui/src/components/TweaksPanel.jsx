import { useState } from 'react'

const HUE_PRESETS = [
  { name: 'phosphor', hue: 145 },
  { name: 'amber',    hue: 78  },
  { name: 'cyan',     hue: 220 },
  { name: 'violet',   hue: 305 },
  { name: 'rose',     hue: 18  },
]

export default function TweaksPanel({ tweaks, onChange }) {
  const [visible, setVisible] = useState(false)
  const set = (k, v) => onChange({ ...tweaks, [k]: v })

  if (!visible) {
    return (
      <button className="tweaks-toggle" onClick={() => setVisible(true)} title="Tweaks">
        ⚙ tweaks
      </button>
    )
  }

  return (
    <div className="tweaks">
      <div className="tweaks-head">
        <h4>Tweaks</h4>
        <button onClick={() => setVisible(false)} aria-label="close">×</button>
      </div>
      <div className="tweaks-body">
        <div className="tweak-row">
          <div className="tweak-label">Density</div>
          <div className="density-toggle">
            <button
              className={tweaks.density === 'comfortable' ? 'active' : ''}
              onClick={() => set('density', 'comfortable')}
            >comfortable</button>
            <button
              className={tweaks.density === 'compact' ? 'active' : ''}
              onClick={() => set('density', 'compact')}
            >compact</button>
          </div>
        </div>

        <div className="tweak-row">
          <div className="tweak-label">Accent hue</div>
          <div className="hues">
            {HUE_PRESETS.map(p => (
              <button
                key={p.hue}
                className={'hue-swatch ' + (tweaks.accentHue === p.hue ? 'active' : '')}
                style={{ background: `oklch(0.78 0.15 ${p.hue})` }}
                title={p.name}
                onClick={() => set('accentHue', p.hue)}
              />
            ))}
          </div>
        </div>

        <div className="tweak-row">
          <div className="tweak-label">Flair</div>
          <div className="toggle-row">
            <span className="toggle-label">bezel scanlines</span>
            <span
              className={'switch ' + (tweaks.scanlines ? 'on' : '')}
              onClick={() => set('scanlines', !tweaks.scanlines)}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
