import type { InstallFragment } from './types'

const pkg = (cli: string) => cli.replace(/^npm install -g /, '')

const HOME_BASH = '$HOME/bubo-docker/home'
const HOME_PWSH = '$env:USERPROFILE\\bubo-docker\\home'

export const docker: InstallFragment = {
  prereqs: () => [
    {
      text: 'Docker — the only host dependency; the agent CLI is baked into the image in step 2, and tokens are passed in as environment variables',
      links: [{ label: 'Docker', href: 'https://docs.docker.com/get-docker/' }],
    },
  ],
  prereqInstall: () => null,
  installCode: (ctx) => {
    const dockerfile = `FROM ghcr.io/mountainowl/bubo
USER root
RUN apt-get update \\
 && apt-get install -y --no-install-recommends nodejs npm \\
 && npm install -g ${pkg(ctx.agentCli)} \\
 && rm -rf /var/lib/apt/lists/*
USER bubo`
    if (ctx.os.shell === 'powershell') {
      return `# bubo's image is bring-your-own-agent — bake the ${ctx.agentCliName} CLI in
$d = "$env:USERPROFILE\\bubo-docker"
New-Item -ItemType Directory -Force -Path "$d\\home" | Out-Null
Set-Location $d
@"
${dockerfile}
"@ | Set-Content -Path Dockerfile -Encoding utf8
docker build -t bubo-local .`
    }
    return `# bubo's image is bring-your-own-agent — bake the ${ctx.agentCliName} CLI in
mkdir -p "$HOME/bubo-docker" && cd "$HOME/bubo-docker"
cat > Dockerfile <<'EOF'
${dockerfile}
EOF
docker build -t bubo-local .`
  },
  initIntro: 'Initialise in the container (its home is persisted on a volume), then write the config:',
  configCode: (ctx) => {
    if (ctx.os.shell === 'powershell') {
      return `New-Item -ItemType Directory -Force -Path "${HOME_PWSH}" | Out-Null
docker run --rm -v "${HOME_PWSH}:/home/bubo" bubo-local bubo init
@"
${ctx.toml}
"@ | Set-Content -Path "${HOME_PWSH}\\.local\\share\\bubo\\config\\env.toml" -Encoding utf8`
    }
    return `mkdir -p "${HOME_BASH}"
docker run --rm -v "${HOME_BASH}:/home/bubo" bubo-local bubo init

cat > "${HOME_BASH}/.local/share/bubo/config/env.toml" <<'EOF'
${ctx.toml}
EOF`
  },
  runCode: (ctx) => {
    const home = ctx.os.shell === 'powershell' ? HOME_PWSH : HOME_BASH
    return `${ctx.os.exportLine(ctx.scmEnv, `<${ctx.scmPrefix}>`)}
${ctx.os.exportLine(ctx.agentKeyEnv, `<your-${ctx.agentKeyName.toLowerCase()} key>`)}

docker run --rm -v "${home}:/home/bubo" -e ${ctx.scmEnv} -e ${ctx.agentKeyEnv} bubo-local bubo-poller`
  },
}
