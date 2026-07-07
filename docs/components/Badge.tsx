import type { ReactNode } from 'react'
import styles from './badge.module.css'

export function Badge({ children, href }: { children: ReactNode; href?: string }) {
  if (href) {
    return (
      <a href={href} className={styles.badge}>
        {children}
      </a>
    )
  }
  return <span className={styles.badge}>{children}</span>
}
