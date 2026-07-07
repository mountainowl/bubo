import styles from './sampleReviews.module.css'

type Line = [string, string]
type Sample = {
  label: string
  caption: string
  tag: string
  muted?: boolean
  file?: string
  add?: string
  title: string
  lines: Line[]
  conf?: string
}

// Synthetic, illustrative reviews — no real repositories, code, or org data.
const SAMPLES: Sample[] = [
  {
    label: 'merge request · inline finding',
    caption: 'A blocking finding, posted inline — structured impact / evidence / fix with a confidence score.',
    tag: 'issue · blocking · security',
    file: 'config/settings.example',
    add: '+ api_token = "••••••••••••••••"',
    title: 'Hard-coded credential committed',
    lines: [
      ['Impact', 'anyone with repo access can use the token until it is revoked.'],
      ['Fix', 'remove it, rotate, and inject via a CI secret.'],
    ],
    conf: '0.98',
  },
  {
    label: 'recall · learning',
    caption: 'Recall & learning — a class your team keeps dismissing stops getting posted.',
    tag: 'style · dismissed',
    muted: true,
    title: 'Naming nit — skipped, not re-posted',
    lines: [['Why', 'this category was dismissed here before; suppression is on.']],
  },
]

export function SampleReviews() {
  return (
    <section className={styles.wrap}>
      <div className={styles.glow} aria-hidden="true" />

      <p className={styles.eyebrow}>See it in action</p>
      <h2 className={styles.title}>What a review looks like</h2>
      <div className={styles.bar} />

      <div className={styles.shots}>
        {SAMPLES.map((s) => (
          <figure key={s.title} className={styles.figure}>
            <div className={styles.ring}>
              <div className={styles.frame}>
                <div className={styles.chrome}>
                  <span className={styles.dots}>
                    <span />
                    <span />
                    <span />
                  </span>
                  <span className={styles.url}>{s.label}</span>
                </div>
                <div className={styles.mock}>
                  {s.file && (
                    <div className={styles.mockDiff}>
                      <span className={styles.mockFile}>{s.file}</span>
                      {s.add && <span className={styles.mockAdd}>{s.add}</span>}
                    </div>
                  )}
                  <div className={styles.mockNote}>
                    <span className={styles.mockAvatar}>LR</span>
                    <div className={styles.mockBody}>
                      <span className={styles.mockMeta}>LLM Reviewer · bot</span>
                      <span
                        className={`${styles.mockTag} ${s.muted ? styles.mockTagMuted : ''}`}
                      >
                        {s.tag}
                      </span>
                      <p className={styles.mockTitle}>{s.title}</p>
                      {s.lines.map(([k, v]) => (
                        <p key={k} className={styles.mockLine}>
                          <span className={styles.mockK}>{k}</span> {v}
                        </p>
                      ))}
                      {s.conf && <span className={styles.mockConf}>confidence {s.conf}</span>}
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <figcaption className={styles.caption}>{s.caption}</figcaption>
          </figure>
        ))}
      </div>
    </section>
  )
}
