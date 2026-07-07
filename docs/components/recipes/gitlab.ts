import { SCM } from '../quickstartShared'
import type { ScmFragment } from './types'

const s = SCM.gitlab

export const gitlab: ScmFragment = {
  prereq: {
    text: 'glab (GitLab CLI), authenticated — clones and fetches merge requests',
    links: [
      { label: 'glab', href: 'https://gitlab.com/gitlab-org/cli#installation' },
      { label: 'token', href: s.tokenUrl },
    ],
  },
  projectPath: s.path,
  configBlock: `[scm]
provider = "gitlab"

[gitlab]
token = "\${${s.env}}"        # ${s.tokenHint}`,
}
