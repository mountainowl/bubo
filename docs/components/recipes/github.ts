import { SCM } from '../quickstartShared'
import type { ScmFragment } from './types'

const s = SCM.github

export const github: ScmFragment = {
  prereq: {
    text: 'gh (GitHub CLI), authenticated — clones and fetches pull requests',
    links: [
      { label: 'gh', href: 'https://cli.github.com/' },
      { label: 'token', href: s.tokenUrl },
    ],
  },
  projectPath: s.path,
  configBlock: `[scm]
provider = "github"

[github]
token = "\${${s.env}}"        # ${s.tokenHint}`,
}
