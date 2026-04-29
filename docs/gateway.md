# `kent gateway` — Discord setup

This walkthrough creates a Discord application, generates a bot token, and invites the bot to a server kent can talk in. It assumes you have admin rights on at least one Discord server.

## Step 1 — Create the Discord application

1. Go to <https://discord.com/developers/applications>.
2. Click **New Application** (top-right). Name it (e.g. `kent`). Accept ToS.

## Step 2 — Add a bot user and grab the token

1. In the left sidebar of your new app, click **Bot**.
2. Click **Reset Token** → **Yes, do it!** → **Copy**. *This is your `discord_bot_token`.* Save it now — Discord won't show it again.
3. Scroll down to **Privileged Gateway Intents**. Toggle ON:
   - **MESSAGE CONTENT INTENT** — required to read message bodies.
   - **PRESENCE INTENT** — required to observe other users' online status.
   - **SERVER MEMBERS INTENT** — required for member lists / mentions resolution.
4. Click **Save Changes**.

## Step 3 — Generate the invite URL

1. Left sidebar → **OAuth2** → **URL Generator**.
2. **Scopes**: check `bot` and `applications.commands`.
3. **Bot Permissions**: View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Manage Threads, Read Message History, Add Reactions, Embed Links, Attach Files, Use Slash Commands.
4. Copy the **Generated URL** at the bottom and open it in your browser. Pick a server you administer and authorize.

## Step 4 — Save the token

```bash
kent gateway config              # interactive: pastes token via getpass, persists with chmod 0600
```

Or paste directly into `~/.kent/credentials.json`:

```json
{ "atlascloud": "apikey-...", "discord_bot_token": "<paste>" }
```

Then `kent gateway start` (detached) or `kent gateway run` (foreground).

## Wing layout

Each Discord channel/DM maps to its own kent memory wing:

- **Guild channel:** `discord_<guild_id>_<channel_id>`
- **DM:** `discord_dm_<user_id>`

Wings are flat names (underscores only — slashes aren't allowed in wing names) and stay under the 64-character cap. Each session has its own `MemPalaceStore`, so concurrent channels never race on the active-wing field.

## Troubleshooting

- **"no Discord bot token"** — run `kent gateway config` and paste the token.
- **bot is online but ignores messages** — make sure the bot has the `MESSAGE CONTENT INTENT` enabled in the developer portal, and either @mention it or pass `--all-messages` when starting the gateway.
- **gateway crashes on start** — check `~/.kent/gateway.log`. Common causes: invalid token (Discord returns 401), missing intents.
- **dev-startup.sh says `gateway disabled`** — `~/.kent/credentials.json` doesn't have a `discord_bot_token` key. Run `kent gateway config`, or add the key to your repo `credentials.json` and re-run `dev-startup.sh`.

## Lifecycle commands

```bash
kent gateway                  # alias for `kent gateway start`
kent gateway run              # foreground (Ctrl-C to stop)
kent gateway start            # detach; writes ~/.kent/gateway.pid
kent gateway stop             # SIGTERM the daemon, await up to 10s, SIGKILL on timeout
kent gateway restart
kent gateway status           # is it running? where's the log?
kent gateway config           # set/reset token + defaults
```

Flags accepted by `run` / `start` / `restart`:

| Flag | Default | What it does |
|---|---|---|
| `--mention-only` | on | Only respond when @-mentioned |
| `--all-messages` | off | Respond to every message in visible channels |
| `--status` | `online` | Initial presence: `online`/`idle`/`dnd`/`invisible` |
| `--activity` | `thinking` | "Playing X" / "Watching X" string |
| `--log-file` | `~/.kent/gateway.log` | Where to write detached stdout/stderr |
