// One recipe = os × platform (scm) × agent × install, assembled at runtime from
// the per-choice fragments in this directory. Each fragment lives in its own
// file and contributes its piece of the steps (prereqs, install, configure, run).

export type ScmId = 'gitlab' | 'github'
export type AgentId = 'codex' | 'claude' | 'selfhosted'
export type InstallId = 'uv' | 'pip' | 'docker'
export type OsId = 'mac' | 'linux' | 'windows'

export type Link = { label: string; href: string }
export type Prereq = { text: string; links?: Link[] }

export type OsFragment = {
  label: string
  icon: string
  shell: 'bash' | 'powershell'
  configPath: string
  // Command to install the host tools (uv/git/node + the agent CLI) on this OS.
  prereqInstall: (agentCli: string) => string
  // How this OS's shell sets an environment variable.
  exportLine: (name: string, placeholder: string) => string
}

export type Ctx = {
  scmEnv: string
  scmPrefix: string
  agentKeyEnv: string
  agentKeyName: string
  agentCli: string
  agentCliName: string
  os: OsFragment
  toml: string
}

export type ScmFragment = {
  prereq: Prereq
  configBlock: string
  projectPath: string
}

export type AgentFragment = {
  prereq: Prereq
  keyEnv: string
  keyName: string
  cli: string
  cliName: string
  configBlock: string
  extra?: { intro: string; code: string }
}

export type InstallFragment = {
  prereqs: (parts: { scm: Prereq; agent: Prereq }) => Prereq[]
  // OS-specific command to install the prerequisites (null when none — e.g.
  // Docker, where the only host dependency is Docker itself).
  prereqInstall?: (ctx: Ctx) => string | null
  installCode: (ctx: Ctx) => string
  initIntro: string
  configCode: (ctx: Ctx) => string
  runCode: (ctx: Ctx) => string
}

export type Recipe = {
  prereqs: Prereq[]
  prereqInstall?: string | null
  installCode: string
  initIntro: string
  configCode: string
  extraConfig?: { intro: string; code: string }
  runCode: string
  // Agentic-install kit: a context file the user fills in + saves, and a prompt
  // they paste into an AI CLI (Claude/Codex) to perform the install.
  contextData: string
  installPrompt: string
}
