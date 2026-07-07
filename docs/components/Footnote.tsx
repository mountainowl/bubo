import type { ReactNode } from 'react'
import styles from './footnote.module.css'

export function Footnote({ id, children }: { id?: string; children: ReactNode }) {
  return (
    <div id={id} className={styles.note}>
      {children}
    </div>
  )
}
