# Anthropic

Anthropic API key + Claude Code coding agent for Settings → LLMs. Install from Store → Gateways, then enable to show Anthropic under Providers & Keys and Claude Code under Coding Agents.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`anthropic`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Two Claude accounts

Settings → LLMs → **Second Claude account** → **Log in account 2** signs a second Claude subscription in. It lives in its own Claude config folder (`%LOCALAPPDATA%\UEFN-Ducky\coding_agents\claude-account-2`, passed to `claude` as `CLAUDE_CONFIG_DIR`), so it never replaces the usual login in `~/.claude`.

Account 2 shares everything but the login with account 1: its folder links account 1's `projects` (sessions), `skills`, `agents`, `commands`, `plugins`, `hooks` and `output-styles`, and copies its `settings.json` and `CLAUDE.md`.

**Pick the account** with the Account 1 / Account 2 toggle under the usage bars in the model picker, or Settings → **Use account**. The bars show the account in use.

With **Switch accounts when one is at its limit** on (the default):

- Before each Claude Code turn the plugin reads the picked account's plan usage (at most once a minute). At 100% of the 5-hour or weekly limit, the turn runs on the other account.
- If a turn hits the limit before Claude did any work, it is replayed once on the other account. If work had started, nothing is replayed and the next message uses the other account.
- Chats go back to the picked account once its limit resets.

Windows only: on macOS Claude Code keeps its login in the Keychain, not in the config folder.

## Build

```bash
py scripts/build_zip.py
```

Writes `deploy/anthropic-1.0.63.ducky-plugin.zip` (scripts/ and deploy/ are not packed).

## Secrets

Never commit tokens or keys. The app stores `anthropic` locally (DPAPI), not in this package.

## License

MIT. Copyright (c) 2026 Mindful Path Company, LLC. See [LICENSE](LICENSE).
