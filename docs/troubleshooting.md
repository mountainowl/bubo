# Troubleshooting

Host and infrastructure issues that present as Bubo misbehaving but are fixed in
the environment, not in Bubo. Each entry is **symptom → cause → fix**. For normal
operation — deploy, schedule, grade, report — see [Operate](operate.md).

## "No findings" on a change that isn't clean

**Symptom**

- A review (usually an MCP-triggered `review_change`) returns no findings on a
  change that is not genuinely clean.
- The transcript or report file shows:
  ```
  bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
  sandbox is rejecting shell execution
  git_status / git_diff / git_show failed
  ```
- An anomalous spike in `no_findings`. Codex exits 0, so nothing flags it.

**Cause**

`[agents].codex_sandbox` (`read-only` or `workspace-write`) isolates the run with
**bubblewrap**, which needs an unprivileged user namespace. An npm/global Codex
ships its own `bwrap` with no AppArmor profile; on Ubuntu with
`kernel.apparmor_restrict_unprivileged_userns = 1` the kernel blocks it, and every
git/shell tool the agent needs fails. The poller usually survives (its diff is
embedded in the prompt); the MCP path fails because it runs Codex nested with no
checkout and must shell out to git. An apt/distro Codex uses the system `bwrap`,
which has an allowed profile — hence "it works when installed via apt".

Confirm the restriction is on:

```sh
sysctl kernel.apparmor_restrict_unprivileged_userns   # 1 = on
```

**Fix — AppArmor distros (Ubuntu, Debian, openSUSE)**

Grant the user-namespace capability to Codex's own bundled `bwrap`. Locate it:

```sh
BWRAP=$(find "$(dirname "$(dirname "$(readlink -f "$(command -v codex)")")")" \
  -path '*codex-resources/bwrap' 2>/dev/null | head -1)
echo "$BWRAP"
```

Install a profile and reload AppArmor:

```sh
PROFILE=/etc/apparmor.d/codex-bwrap
sudo tee "$PROFILE" >/dev/null <<EOF
abi <abi/4.0>,
include <tunables/global>
$BWRAP flags=(default_allow) {
  userns,
  include if exists <local/codex-bwrap>
}
EOF
sudo apparmor_parser -r "$PROFILE"
```

Verify:

```sh
"$BWRAP" --unshare-net --ro-bind / / --proc /proc --dev /dev /bin/echo ok   # → ok
codex exec --profile bubo --skip-git-repo-check --ephemeral 'run: echo ok'  # → ok
```

- If `apparmor_parser --version` is < 4.0, set `abi <abi/4.0>` to your parser's
  abi (e.g. `abi <abi/3.0>` on Ubuntu 22.04) or drop the `abi` line.
- A Codex upgrade can move the bundled `bwrap`; re-run the locate step and
  `apparmor_parser -r`.

**Fix — non-AppArmor hosts**

- **RHEL / Fedora / Rocky / Alma (SELinux):** the AppArmor sysctl and profile
  don't apply. User namespaces are usually allowed; if not, check
  `sysctl user.max_user_namespaces` (must be `> 0`).
- **Arch / other:** userns usually works out of the box; if `bwrap` still fails,
  confirm `kernel.unprivileged_userns_clone = 1`.

**Alternatives (no profile)**

- `sudo apt install bubblewrap` (Debian/Ubuntu) — Codex prefers a `bwrap` on
  `PATH`, and the distro package ships an allowed profile. Confirm with
  `command -v bwrap` → `/usr/bin/bwrap`.
- On a dedicated, credential-stripped host, set
  `codex_sandbox = "danger-full-access"` in `[agents]`. **Not** `workspace-write`
  — it is also bubblewrap-based and fails identically.
- Avoid disabling `kernel.apparmor_restrict_unprivileged_userns` host-wide unless
  the host is fully isolated.

**Make it visible**

Because this hides as "no findings", ship poller stdout to your observability
stack and alert on an anomalous no-findings rate (see
[Metrics & telemetry](telemetry.md)). To check a single run by hand, grep its
report file for `bwrap:`.
