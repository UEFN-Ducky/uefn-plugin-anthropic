# Anthropic

Anthropic API key + Claude Code coding agent for Settings → LLMs. Install from Store → Gateways, then enable to show Anthropic under Providers & Keys and Claude Code under Coding Agents.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`anthropic`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Several Claude accounts and API keys

Settings → LLMs → Claude Code → **Accounts** holds any number of Claude logins; Anthropic → **Accounts** holds any number of API keys. Ducky's core owns the list; this plugin tells it how to run Claude Code as each login.

Each extra login lives in its own Claude config folder (`%LOCALAPPDATA%\UEFN-Ducky\coding_agents\accounts\claude_code\<id>`, passed to `claude` as `CLAUDE_CONFIG_DIR`), so it never replaces the usual login in `~/.claude`. It shares everything but the login with `~/.claude`: `projects` (sessions), `skills`, `agents`, `commands`, `plugins`, `hooks` and `output-styles` are linked, `settings.json` and `CLAUDE.md` copied. A login from 1.0.63's "Second Claude account" moves into the list once, as "Account 2".

**Pick the account** with the buttons under the usage bars in the model picker, or in Settings. The bars are labelled with the account in use.

With **Switch accounts when one is at its limit** on (the default):

- A Claude Code turn that hits the 5-hour or weekly limit before Claude did any work is replayed once on the next account, with the conversation carried over. If work had started, nothing is replayed and the next message uses the next account.
- An API-key turn that gets a 429, a rate-limit or a "credit balance is too low" error before any output moves to the next key.
- Chats go back to the picked account once its limit resets.

## Build

```bash
py scripts/build_zip.py
```

Writes `deploy/anthropic-1.0.63.ducky-plugin.zip` (scripts/ and deploy/ are not packed).

## Secrets

Never commit tokens or keys. The app stores `anthropic` locally (DPAPI), not in this package.

## License

MIT. Copyright (c) 2026 Mindful Path Company, LLC. See [LICENSE](LICENSE).
