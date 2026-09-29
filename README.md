# Moria home bot

A Claude-powered bot for Home Assistant that you text in a private Discord channel.

```
you (Discord #home) ──> homebot container ──> Claude API
                               │
                               └──> Home Assistant REST API
```

The container only makes outgoing connections to Discord, Anthropic and Home Assistant, so you don't need port forwarding.

## What it does

- **Controls the house.** "Turn off the downstairs lights", "set the office to 70", "is the garage open?"
- **Asks before sensitive actions.** Service calls on locks and alarm panels, or on any entity whose id or name contains a word like `garage`, `oven` or `door`, post **Approve / Deny** buttons in the channel. Only allowed users can press them. Unanswered requests are denied after 5 minutes.
- **Refuses anything outside an allowlist of domains.** For example, it can't restart Home Assistant or run shell commands.
- **Remembers things.** Say "remember that bedtime means…" and it appends the fact to `workspace/notes.md`. Notes load at the start of each new conversation.
- **Runs routines.** A nightly check at 22:30 messages you only if something's left on, open or unlocked. It never changes anything by itself, but you can reply "lock it" to act on its report. An optional morning summary is included too.

A conversation resets after 30 minutes of quiet, or when you type `!reset`.

## Setup

### 1. Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application** → name it "Home Bot".
2. **Bot** tab → **Reset Token** → copy it into `DISCORD_BOT_TOKEN`.
3. On the same tab, turn on **Message Content Intent**. Without it, the bot can't read what you type.
4. **OAuth2 → URL Generator**: tick scope `bot`, then the permissions *View Channels*, *Send Messages* and *Read Message History*. Open the generated URL and add the bot to your private server.
5. In Discord, go to **Settings → Advanced** and turn on **Developer Mode**. Then right-click the `#home` channel → **Copy Channel ID** (`DISCORD_CHANNEL_ID`). Right-click yourself (and your wife) → **Copy User ID** (`DISCORD_ALLOWED_USER_IDS`, comma-separated).

### 2. Home Assistant token

In Home Assistant, open your **Profile → Security → Long-lived access tokens → Create token**. Put it in `HOMEASSISTANT_TOKEN`. Set `HOMEASSISTANT_URL` to HA's LAN address (e.g. `http://192.168.1.10:8123`). `localhost` won't work from inside the container.

### 3. Claude API key

Create a key at <https://console.anthropic.com> and put it in `ANTHROPIC_API_KEY`. Set a monthly spend limit there as well.

### 4. Run it

```sh
cp .env.example .env        # fill it in
sudo chown -R 1000:1000 workspace   # the container runs as uid 1000 and writes notes.md
docker compose up -d --build
docker compose logs -f
```

On start it logs `connected to Home Assistant …` and `logged in as …`. If Home Assistant is unreachable or the token is wrong, it exits with an error.

Then fill in `workspace/role.md` with room names and what phrases like "bedtime" mean. The more specific it is, the fewer questions the bot has to ask. Changes apply to the next conversation; no rebuild needed.

## Configuration

All settings live in `.env`; see `.env.example`.

| Setting | Default | Notes |
|---|---|---|
| `CLAUDE_MODEL` | `claude-opus-5-5` | `claude-sonnet-5-5` or `claude-haiku-4-5` are cheaper; Haiku is plenty for switching lights. |
| `CLAUDE_EFFORT` | `low` | How hard the model thinks. `low` suits home control. Ignored for Haiku. |
| `NIGHTLY_CHECK_TIME` / `MORNING_SUMMARY_TIME` | `22:30` / `off` | 24h local time (`TZ`), or `off`. |
| `SENSITIVE_DOMAINS` | `lock,alarm_control_panel` | Every call in these domains needs approval. |
| `SENSITIVE_KEYWORDS` | `lock,garage,alarm,oven,stove,door,gate,security` | Matched against entity id and friendly name. |
| `ALLOWED_DOMAINS` | lights, switches, climate, covers, locks, media, scenes, scripts, vacuum… | Anything else is refused. |

**About scripts and scenes.** Scripts and scenes are allowed, but they're only gated by their own name. If a script unlocks a door, give it a name containing a sensitive keyword, or remove `script` from `ALLOWED_DOMAINS`.

On Opus/Sonnet, refusal fallback is turned on (`fallbacks: "default"`). If the model declines a request, the API retries it on a fallback model instead of failing.

## Development

```sh
pip install -e '.[dev]'
pytest
```

Code layout:

- `src/homebot/tools.py`: Home Assistant tools and the safety/approval rules
- `src/homebot/agent.py`: the Claude tool-use loop
- `src/homebot/discord_bot.py`: the Discord front end, approval buttons and routines
- `src/ha_mcp/ha_client.py`: the Home Assistant REST client
