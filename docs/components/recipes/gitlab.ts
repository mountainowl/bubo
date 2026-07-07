import { SCM } from '../quickstartShared'
import type { ScmFragment } from './types'

const s = SCM.gitlab

export const gitlab: ScmFragment = {
  prereq: {
    text: 'A GitLab token with API scope — clones over HTTPS and drives the REST API',
    links: [{ label: 'token', href: s.tokenUrl }],
  },
  projectPath: s.path,
  configBlock: `[scm]
provider = "gitlab"

[gitlab]
token = "\${${s.env}}"        # ${s.tokenHint}`,
}
