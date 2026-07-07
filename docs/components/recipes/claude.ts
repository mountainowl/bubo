import { AGENT } from '../quickstartShared'
import type { AgentFragment } from './types'

const a = AGENT.claude

export const claude: AgentFragment = {
  prereq: {
    text: `Claude CLI (\`${a.cli}\`), authenticated to ${a.keyName}`,
    links: [
      { label: 'Claude CLI', href: 'https://docs.anthropic.com/en/docs/claude-code/overview' },
      { label: 'API keys', href: a.keyUrl },
    ],
  },
  keyEnv: a.keyEnv,
  keyName: a.keyName,
  cli: a.cli,
  cliName: a.cliName,
  configBlock: `[agents]
llm_model        = "${a.model}"
llm_api_key_env  = "${a.keyEnv}"
reviewer_command = ["claude", "-p"]   # Claude is driven via its CLI`,
}
