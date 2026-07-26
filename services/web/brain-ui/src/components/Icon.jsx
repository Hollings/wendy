// Tiny icon set — monoline, 16x16 viewBox, currentColor.

export default function Icon({ name, size = 14 }) {
  const common = {
    width: size, height: size, viewBox: '0 0 16 16',
    fill: 'none', stroke: 'currentColor', strokeWidth: 1.2,
    strokeLinecap: 'round', strokeLinejoin: 'round',
  }
  switch (name) {
    case 'Read':
      return <svg {...common}><path d="M3 3h6l3 3v7H3z"/><path d="M9 3v3h3"/><path d="M5 8h5M5 10.5h4"/></svg>
    case 'Write':
      return <svg {...common}><path d="M3 3h7v10H3z"/><path d="M5 6h4M5 8.5h4M5 11h3"/></svg>
    case 'Edit':
      return <svg {...common}><path d="M11 3l2 2-7 7-3 1 1-3z"/><path d="M9.5 4.5l2 2"/></svg>
    case 'Bash':
      return <svg {...common}><rect x="2" y="3" width="12" height="10" rx="1"/><path d="M4.5 7L6.5 8.5 4.5 10M8 10.5h3.5"/></svg>
    case 'Grep':
      return <svg {...common}><circle cx="7" cy="7" r="3.5"/><path d="M9.5 9.5L13 13"/></svg>
    case 'Glob':
      return <svg {...common}><path d="M3 8h10M5 4.5l-2 3.5 2 3.5M11 4.5l2 3.5-2 3.5"/></svg>
    case 'Task':
      return <svg {...common}><circle cx="8" cy="8" r="5"/><circle cx="8" cy="8" r="1.5"/><path d="M8 3v1M8 12v1M3 8h1M12 8h1"/></svg>
    case 'TodoWrite':
      return <svg {...common}><path d="M3 4.5h8M3 8h8M3 11.5h5"/><path d="M13 4l-1 1L11 4"/></svg>
    case 'WebSearch':
      return <svg {...common}><circle cx="8" cy="8" r="4.5"/><path d="M3.5 8h9M8 3.5c2 2 2 7 0 9M8 3.5c-2 2-2 7 0 9"/></svg>
    case 'WebFetch':
      return <svg {...common}><circle cx="6.5" cy="6.5" r="3.5"/><path d="M9 9l4 4M13 13h-2.5M13 13v-2.5"/></svg>
    case 'Tool':
      return <svg {...common}><rect x="3" y="3" width="10" height="10" rx="1"/><path d="M6 8h4M8 6v4"/></svg>
    case 'Thinking':
      return <svg {...common}><path d="M8 3.2C5 3.2 3.2 5 3.2 7.5c0 1.4.8 2.6 2 3.4L5 13l2.3-1.4c.2 0 .4 0 .7 0 3 0 4.8-1.8 4.8-4.2S11 3.2 8 3.2z"/></svg>
    case 'System':
      return <svg {...common}><rect x="2.5" y="4" width="11" height="8" rx="1"/><path d="M5 7h1M8 7h3M5 9.5h2M9 9.5h2"/></svg>
    case 'Result':
      return <svg {...common}><path d="M3 8l3 3 7-7"/></svg>
    case 'End':
      return <svg {...common}><circle cx="8" cy="8" r="5"/><path d="M6 8h4"/></svg>
    case 'Live':
      return <svg {...common}><circle cx="8" cy="8" r="2" fill="currentColor" stroke="none"/><path d="M4.5 4.5a5 5 0 0 0 0 7M11.5 4.5a5 5 0 0 1 0 7M2.5 2.5a8 8 0 0 0 0 11M13.5 2.5a8 8 0 0 1 0 11"/></svg>
    case 'Speech':
      return <svg {...common}><path d="M2.5 4.5h11v6h-6l-3 2.5V10.5h-2z"/><path d="M5 7h6"/></svg>
    case 'Discord':
      return <svg {...common}><path d="M2.5 4h11v6.5h-6l-3 2.5V10.5h-2z"/><path d="M5.5 6.5L7.5 7.2 5.5 8"/><path d="M8.5 8.3h2.5"/></svg>
    case 'Reaction':
      return <svg {...common}><circle cx="8" cy="8" r="5"/><path d="M6 6.5h.01M10 6.5h.01"/><path d="M5.8 9.3a3 3 0 0 0 4.4 0"/></svg>
    case 'Attach':
      return <svg {...common}><path d="M10.5 4.5l-5 5a1.8 1.8 0 0 0 2.5 2.5l5-5a3 3 0 0 0-4.2-4.2l-5 5"/></svg>
    case 'Inbox':
      return <svg {...common}><path d="M2.5 9l1.5-5h8l1.5 5v3.5h-11z"/><path d="M2.5 9h3l1 1.5h3L10.5 9h3"/></svg>
    case 'Bell':
      return <svg {...common}><path d="M4.5 11V7.5a3.5 3.5 0 0 1 7 0V11l1 1.5h-9z"/><path d="M6.8 14h2.4"/></svg>
    case 'Limit':
      return <svg {...common}><path d="M4 3h8M4 13h8"/><path d="M5.5 3v2L8 7.5 10.5 5V3M5.5 13v-2L8 8.5 10.5 11v2"/></svg>
    case 'Compact':
      return <svg {...common}><path d="M3 3h10M3 13h10"/><path d="M8 5v2.5M8 11V8.5"/><path d="M6 6.5L8 4.5l2 2M6 9.5L8 11.5l2-2"/></svg>
    case 'Status':
      return <svg {...common}><circle cx="8" cy="8" r="5"/><path d="M8 5v3l2 1.5"/></svg>
    case 'Unknown':
      return <svg {...common}><circle cx="8" cy="8" r="5.5"/><path d="M6.4 6.4a1.6 1.6 0 1 1 1.9 1.9v1"/><path d="M8.3 11h0"/></svg>
    default:
      return <svg {...common}><circle cx="8" cy="8" r="3"/></svg>
  }
}
