import React from 'react'
import { asset } from './asset'

export function SiteFooter() {
  return (
    <div
      style={{
        width: '100%',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '0.6rem',
        padding: '2.75rem 1rem',
      }}
    >
      <a
        href="https://github.com/mountainowl"
        target="_blank"
        rel="noreferrer"
        aria-label="MountainOwl"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '0.6rem',
          textDecoration: 'none',
        }}
      >
        {/* logomark — the actual mushroom photo, background removed */}
        <img
          src={asset('/mushroom-logo.png')}
          alt="MountainOwl"
          width={45}
          height={34}
          style={{
            display: 'block',
            height: '34px',
            width: 'auto',
            transform: 'scaleX(-1)',
            filter: 'drop-shadow(0 1px 3px rgba(0, 0, 0, 0.2))',
          }}
        />
        {/* wordmark */}
        <span
          style={{
            fontFamily: "'Space Grotesk', ui-sans-serif, system-ui, sans-serif",
            fontSize: '1.5rem',
            fontWeight: 700,
            letterSpacing: '-0.02em',
            lineHeight: 1,
          }}
        >
          <span style={{ color: '#94a3b8', fontWeight: 600 }}>Mountain</span>
          <span style={{ color: '#2563eb' }}>Owl</span>
        </span>
      </a>
      <div
        style={{
          fontSize: '0.75rem',
          letterSpacing: '0.04em',
          color: '#9ca3af',
          fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
        }}
      >
        Bubo · MIT licensed · © 2026
      </div>
    </div>
  )
}
