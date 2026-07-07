import type { InstallFragment } from './types'

export const pip: InstallFragment = {
  prereqs: ({ scm, agent }) => [
    {
      text: 'uv (Python 3.14+)',
      links: [{ label: 'uv', href: 'https://docs.astral.sh/uv/getting-started/installation/' }],
    },
    { text: 'Git', links: [{ label: 'Git', href: 'https://git-scm.com/downloads' }] },
    scm,
    agent,
  ],
  prereqInstall: (ctx) => ctx.os.prereqInstall(ctx.agentCli),
  installCode: () => `pip install bubo`,
  initIntro:
    'Run bubo init --no-agent-config to seed the config, then open it and fill in the minimum:',
  configCode: (ctx) => `# ${ctx.os.configPath}
${ctx.toml}`,
  runCode: (ctx) => `${ctx.os.exportLine(ctx.scmEnv, `<${ctx.scmPrefix}>`)}
${ctx.os.exportLine(ctx.agentKeyEnv, `<your-${ctx.agentKeyName.toLowerCase()} key>`)}

bubo init        # template the agent profile from the config you just edited
${ctx.agentAuth}
bubo doctor      # checks workspace, config, DB, and the agent profile
bubo-poller      # one poll cycle — dry-run by default, so it posts nothing`,
}
