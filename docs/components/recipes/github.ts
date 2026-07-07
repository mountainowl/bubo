import { SCM } from '../quickstartShared'
import type { ScmFragment } from './types'

const s = SCM.github

export const github: ScmFragment = {
  prereq: {
    text: 'A GitHub token with pull-request read + write — clones over HTTPS and drives the REST API',
    links: [{ label: 'token', href: s.tokenUrl }],
  },
  projectPath: s.path,
  configBlock: `[scm]
provider = "github"

[github]
token = "\${${s.env}}"        # ${s.tokenHint}`,
}
