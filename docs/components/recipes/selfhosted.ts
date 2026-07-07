import { AGENT } from '../quickstartShared'
import type { AgentFragment } from './types'

// Self-hosted / OpenAI-compatible: Bubo still shells out to Codex, but the agent
// CLI is pointed at your own gateway (Azure OpenAI, vLLM/TGI, LiteLLM, internal
// proxy). The endpoint lives in the *Codex profile*, not env.toml — so this
// fragment carries an `extra` block for that.
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
llm_model       = "<internal-model-name>"   # matches the profile; cost label only
llm_api_key     = "\${LLM_API_KEY}"          # Bubo re-exports it under llm_api_key_env
llm_api_key_env = "OPENAI_API_KEY"          # the env var your gateway/CLI reads
codex_profile   = "bubo"`,
  extra: {
    intro:
      'Point the agent at your gateway — add a model-provider to the Codex profile (config.toml in the agent home: ~/.codex/ for a local install, the mounted home for Docker):',
    code: `[profiles.bubo]
model          = "<internal-model-name>"
model_provider = "inhouse"
sandbox_mode   = "read-only"

[model_providers.inhouse]
name     = "In-house gateway"
base_url = "https://llm.corp.internal/v1"   # your OpenAI-compatible endpoint
env_key  = "OPENAI_API_KEY"
wire_api = "chat"                           # "chat" or "responses" — match your gateway`,
  },
}
