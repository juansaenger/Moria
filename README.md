# Moria home bot

A Claude-powered bot for Home Assistant that you text in a private Discord channel.

```
you (Discord #home) ──> homebot container ──> Claude API
                               │
                               ├──> Home Assistant REST API
                               └──> Seerr / Sonarr / Radarr / qBittorrent (optional)
```

The container only makes outgoing connections to Discord, Anthropic and Home Assistant, so you don't need port forwarding.

## What it does

- **Controls the house.** "Turn off the downstairs lights", "set the office to 70", "is the garage open?"
- **Asks before sensitive actions.** Service calls on locks and alarm panels, or on any entity whose id or name contains a word like `garage`, `oven` or `door`, post **Approve / Deny** buttons in the channel. Only allowed users can press them. Unanswered requests are denied after 5 minutes.
- **Refuses anything outside an allowlist of domains.** For example, it can't restart Home Assistant or run shell commands.
- **Remembers things.** Say "remember that bedtime means…" and it appends the fact to `workspace/notes.md`. Notes load at the start of each new conversation.
- **Looks after Plex downloads (optional).** With Seerr, Sonarr, Radarr and qBittorrent keys in `homebot.env`, it answers "where is the show I asked for?", spots stuck or stalled downloads, triggers searches, finds and grabs releases (including whole-series packs that Sonarr won't take on its own), and removes bad downloads. Grabs over 40GB, removals and torrent deletions post Approve / Deny buttons first.
- **Runs routines.** A nightly check at 22:30 messages you only if something's left on, open or unlocked, or (with media tools on) a request has gone a day without progress. It never changes anything by itself, but you can reply "lock it" to act on its report. An optional morning summary is included too.

A conversation resets after 30 minutes of quiet, or when you type `!reset`.

## Setup

You only need to touch the server once. Everything else happens on your normal computer.

1. **Fill in `homebot.env`.** Copy [`homebot.env.example`](homebot.env.example) to a file named `homebot.env`. Each line has a comment saying where to find the value. The Discord values come from the steps below.
2. **Upload it to the server**, e.g. through the ZimaOS Files app or a network share, into a folder like `/DATA/AppData/homebot`.
3. **Run the installer once over SSH**, from that folder:

   ```sh
   cd /DATA/AppData/homebot
   curl -fsSL https://raw.githubusercontent.com/juansaenger/Moria/claude/sharp-allen-t1cp76/install.sh | sh
   ```

   It checks that nothing required is blank, downloads the bot into `app/`, builds it and starts it. Run the same command again to update after changing `homebot.env` or pulling new code; your `role.md` and notes are kept.
4. **Describe your house** in `app/workspace/role.md` (room names, what "bedtime" means). Changes apply to the next conversation.

### Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application** → name it "Home Bot".
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_BOT_TOKEN`.
3. On the same tab, turn on **Message Content Intent** and click Save. Without it, the bot can't read what you type.
4. **OAuth2 → URL Generator**: tick scope `bot`, then the permissions *View Channels*, *Send Messages* and *Read Message History*. Open the generated URL and add the bot to your server.
5. In Discord, go to **User Settings → Advanced** and turn on **Developer Mode**. Right-click `#homeassistant` → **Copy Channel ID** (`DISCORD_CHANNEL_ID`). Right-click yourself (and your wife) → **Copy User ID** (`DISCORD_ALLOWED_USER_IDS`, comma-separated).

### If it doesn't start

`cd app && docker compose logs -f` shows what's wrong. At startup the bot checks that it can reach Home Assistant and that all settings are there, and says which one is off.

## Configuration

All settings live in `homebot.env`; see `homebot.env.example`.

| Setting | Default | Notes |
|---|---|---|
| `CLAUDE_MODEL` | `claude-opus-5-5` | `claude-sonnet-5-5` or `claude-haiku-4-5` are cheaper; Haiku is plenty for switching lights. |
| `CLAUDE_EFFORT` | `low` | How hard the model thinks. `low` suits home control. Ignored for Haiku. |
| `NIGHTLY_CHECK_TIME` / `MORNING_SUMMARY_TIME` | `22:30` / `off` | 24h local time (`TZ`), or `off`. |
| `SENSITIVE_DOMAINS` | `lock,alarm_control_panel` | Every call in these domains needs approval. |
| `SENSITIVE_KEYWORDS` | `lock,garage,alarm,oven,stove,door,gate,security` | Matched against entity id and friendly name. |
| `ALLOWED_DOMAINS` | lights, switches, climate, covers, locks, media, scenes, scripts, vacuum… | Anything else is refused. |
| `SONARR_URL` / `SONARR_API_KEY` | blank | Series library, queue, searches and grabs. |
| `RADARR_URL` / `RADARR_API_KEY` | blank | Same for movies. |
| `SEERR_URL` / `SEERR_API_KEY` | blank | Who requested what, and whether it's available yet. |
| `QBIT_URL` / `QBIT_USERNAME` / `QBIT_PASSWORD` | blank | Live torrent states, reannounce/recheck/pause/resume, delete (with approval). |

A media service whose URL or key is blank is simply left out; the bot only gets tools for the services it can reach. Startup logs say which ones answered.

**About scripts and scenes.** Scripts and scenes are allowed, but they're only gated by their own name. If a script unlocks a door, give it a name containing a sensitive keyword, or remove `script` from `ALLOWED_DOMAINS`.

On Opus/Sonnet, refusal fallback is turned on (`fallbacks: "default"`). If the model declines a request, the API retries it on a fallback model instead of failing.

## Development

```sh
pip install -e '.[dev]'
pytest
```

Code layout:

- `src/homebot/tools.py`: Home Assistant tools and the safety/approval rules
- `src/homebot/media.py`: Seerr, Sonarr/Radarr and qBittorrent clients and tools
- `src/homebot/agent.py`: the Claude tool-use loop
- `src/homebot/discord_bot.py`: the Discord front end, approval buttons and routines
- `src/ha_mcp/ha_client.py`: the Home Assistant REST client

## Follow-ups

The bot can schedule a one-off check for itself, for example "see whether that
torrent started in 30 minutes". Pending follow-ups live in
`workspace/followups.json`, so they survive a restart or a rebuild, and arrive as
a message from "Follow-up". Ask it to list or cancel them in plain language.
No new settings: times use the `TZ` you already set.

## Automations

The bot can list and read your Home Assistant automations, and create, edit or
delete them. Reads are free. Any change shows you the YAML, as a diff when it is
an edit, and waits for the Approve button; Home Assistant reloads on its own
afterwards. Only automations stored in `automations.yaml` can be edited, which is
everything created in the UI and most hand-written ones. No new settings.
