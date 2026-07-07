import { AGENT } from '../quickstartShared'
import type { AgentFragment } from './types'

const a = AGENT.codex

export const codex: AgentFragment = {
  prereq: {
    text: `Codex CLI (\`${a.cli}\`), authenticated to ${a.keyName}`,
    links: [
      { label: 'Codex CLI', href: 'https://github.com/openai/codex' },
      { label: 'API keys', href: a.keyUrl },
    ],
  },
  keyEnv: a.keyEnv,
  keyName: a.keyName,
  cli: a.cli,
  cliName: a.cliName,
  configBlock: `[agents]
llm_model   = "${a.model}"
llm_api_key = "\${${a.keyEnv}}"   # your ${a.keyName} key, exported as LLM_API_KEY`,
}
