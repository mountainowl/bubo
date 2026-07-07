import React from 'react'
import { DocsThemeConfig } from 'nextra-theme-docs'
import { SiteFooter } from './components/SiteFooter'
import { asset } from './components/asset'

const config: DocsThemeConfig = {
  logo: (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '0.55rem',
        fontWeight: 700,
        textAlign: 'left',
      }}
    >
      <span>
        BUBO<span style={{ opacity: 0.6, fontWeight: 400 }}>docs</span>
      </span>
      <span
        style={{
          fontSize: '0.68rem',
          fontWeight: 600,
          lineHeight: 1,
          padding: '0.2rem 0.5rem',
          borderRadius: '999px',
          background: 'rgba(37, 99, 235, 0.15)',
          color: '#3b82f6',
          border: '1px solid rgba(59, 130, 246, 0.35)',
          fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
        }}
      >
        v0.24.2
      </span>
    </span>
  ),
  navbar: {
    extraContent: (
      <div style={{ display: 'inline-flex', alignItems: 'center', gap: '0.9rem' }}>
        <a
          href="https://github.com/mountainowl/bubo"
          target="_blank"
          rel="noreferrer"
          aria-label="GitHub"
          title="GitHub"
        >
          <img
            src={asset('/github.svg')}
            alt="GitHub"
            width={22}
            height={22}
            style={{ display: 'block' }}
          />
        </a>
        <a
          href="https://pypi.org/project/bubo/"
          target="_blank"
          rel="noreferrer"
          aria-label="PyPI"
          title="PyPI"
        >
          <img
            src={asset('/pypi.svg')}
            alt="PyPI"
            width={22}
            height={22}
            style={{ display: 'block' }}
          />
        </a>
        <a
          href="https://github.com/mountainowl/bubo/pkgs/container/bubo"
          target="_blank"
          rel="noreferrer"
          aria-label="Docker image"
          title="Docker (GHCR)"
        >
          <img
            src={asset('/docker.svg')}
            alt="Docker"
            width={22}
            height={22}
            style={{ display: 'block' }}
          />
        </a>
      </div>
    ),
  },
  docsRepositoryBase: 'https://github.com/mountainowl/bubo/tree/main/docs',
  footer: {
    component: <SiteFooter />,
  },
  head: (
    <>
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
      <link
        href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap"
        rel="stylesheet"
      />
      <meta property="og:title" content="Bubo — docs" />
      <meta
        property="og:description"
        content="Agentic AI code review — with the LLM of your choice."
      />
    </>
  ),
}

export default config
