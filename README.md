# Anthropic

Anthropic API key + Claude Code coding agent for Settings → LLMs. Install from Store → Gateways, then enable to show Anthropic under Providers & Keys and Claude Code under Coding Agents.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`anthropic`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Two Claude accounts

Settings → LLMs → **Second Claude account** → **Log in account 2** signs a second Claude subscription in. It lives in its own Claude config folder (`%LOCALAPPDATA%\UEFN-Ducky\coding_agents\claude-account-2`, passed to `claude` as `CLAUDE_CONFIG_DIR`), so it never replaces the usual login in `~/.claude`.

With **Switch to account 2 when account 1 is at its limit** on (the default):

- Before each Claude Code turn the plugin reads account 1's plan usage (at most once a minute). At 100% of the 5-hour or weekly limit, the turn runs on account 2.
- If a turn hits the limit before Claude did any work, it is replayed once on account 2. If work had started, nothing is replayed and the next message uses account 2.
- Chats go back to account 1 once its limit resets. The chat's Claude session is copied between the two folders so the conversation carries on.

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
