import { AGENT } from '../quickstartShared'
import type { AgentFragment } from './types'

// Self-hosted / OpenAI-compatible: Bubo still shells out to Codex, but points the
// agent at your own gateway (Azure OpenAI, vLLM/TGI, LiteLLM, internal proxy) via
// `llm_base_url` in env.toml. `bubo init` templates the matching model-provider
// block into the Codex profile, and the agent reads LLM_API_KEY from its
// environment at request time.
export const selfhosted: AgentFragment = {
  prereq: {
    text: 'Codex CLI (Bubo shells out to it) and an OpenAI-compatible gateway — Azure OpenAI, vLLM/TGI, LiteLLM, or an internal proxy',
    links: [
      { label: 'Codex CLI', href: 'https://github.com/openai/codex' },
      { label: 'OpenAI-compatible config', href: '/configuration' },
    ],
  },
  keyEnv: 'LLM_API_KEY',
  keyName: 'gateway',
  cli: AGENT.codex.cli,
  cliName: 'Codex',
  configBlock: `[agents]
llm_model    = "<internal-model-name>"          # your gateway's model id; also the cost label
llm_api_key  = "\${LLM_API_KEY}"                 # read from the environment at request time
llm_base_url = "https://llm.corp.internal/v1"   # your OpenAI-compatible endpoint
codex_profile = "bubo"`,
  extra: {
    intro:
      'With `llm_base_url` set, `bubo init` writes this model-provider block into the Codex profile (config.toml in the agent home: ~/.codex/ for a local install, the mounted home for Docker) — no manual editing needed:',
    code: `[model_providers.bubo]
name     = "bubo custom endpoint"
base_url = "https://llm.corp.internal/v1"   # from llm_base_url
env_key  = "LLM_API_KEY"
wire_api = "chat"`,
  },
}
