#!/bin/sh
# Installs or updates the home bot in the current folder.
#
#   1. Put your filled-in homebot.env in a folder on the server.
#   2. From that folder, run:
#      curl -fsSL https://raw.githubusercontent.com/juansaenger/Moria/claude/sharp-allen-t1cp76/install.sh | sh
#
# Run the same command again later to update. Your role.md and notes.md are kept.
set -eu

BRANCH="${HOMEBOT_BRANCH:-claude/sharp-allen-t1cp76}"
ARCHIVE="https://github.com/juansaenger/Moria/archive/refs/heads/$BRANCH.tar.gz"
HERE="$(pwd)"
ENV_FILE="$HERE/homebot.env"
APP="$HERE/app"
REQUIRED="ANTHROPIC_API_KEY DISCORD_BOT_TOKEN DISCORD_CHANNEL_ID DISCORD_ALLOWED_USER_IDS HOMEASSISTANT_URL HOMEASSISTANT_TOKEN TZ"

fail() { echo "ERROR: $*" >&2; exit 1; }

# Docker: prefer running as yourself (docker group). Only fall back to sudo when that
# doesn't work. On ZimaOS the root filesystem is read-only, so docker under sudo can't
# even write its own config, and a plain user in the docker group is the way to go.
SUDO=""
if ! docker ps >/dev/null 2>&1; then
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && sudo docker ps >/dev/null 2>&1; then
        SUDO="sudo"
    else
        fail "Can't talk to Docker. Add your user to the docker group, or run this as root."
    fi
fi

# Docker keeps its config (and looks for the compose plugin) under $DOCKER_CONFIG.
# When that folder isn't writable (ZimaOS: $HOME is /DATA), point it at our own.
if [ -z "${DOCKER_CONFIG:-}" ] && ! mkdir -p "${HOME:-/}/.docker" 2>/dev/null; then
    export DOCKER_CONFIG="$HERE/.docker"
    mkdir -p "$DOCKER_CONFIG"
fi

if $SUDO docker compose version >/dev/null 2>&1; then
    COMPOSE="$SUDO docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="$SUDO docker-compose"
else
    # The compose plugin is often installed but not on docker's search path.
    for plugin in /usr/lib/docker/cli-plugins/docker-compose /usr/libexec/docker/cli-plugins/docker-compose \
                  /usr/local/lib/docker/cli-plugins/docker-compose; do
        if [ -x "$plugin" ]; then COMPOSE="$SUDO $plugin"; break; fi
    done
    [ -n "${COMPOSE:-}" ] || fail "Docker Compose not found on this server."
fi

[ -f "$ENV_FILE" ] || fail "No homebot.env in $HERE. Upload it here, or cd to the folder that has it."

# Files edited on Windows have \r line endings, which would end up inside the values.
env_clean="$(mktemp)"
tr -d '\r' < "$ENV_FILE" > "$env_clean"

missing=""
for key in $REQUIRED; do
    value="$(grep -E "^$key=" "$env_clean" | tail -n 1 | cut -d= -f2-)"
    [ -n "$value" ] || missing="$missing $key"
done
if [ -n "$missing" ]; then
    rm -f "$env_clean"
    fail "These are still blank in homebot.env:$missing"
fi
if grep -Eq '^[A-Z_]+=["'"'"']' "$env_clean"; then
    echo "WARNING: some values in homebot.env are wrapped in quotes. Remove the quotes if the bot can't log in." >&2
fi

echo "Downloading the home bot ($BRANCH)..."
src="$(mktemp -d)"
curl -fsSL "$ARCHIVE" | tar -xz -C "$src" --strip-components=1 || fail "Download failed. Is the server online?"

mkdir -p "$APP"
if [ -d "$APP/workspace" ]; then
    rm -rf "$src/workspace"  # keep your existing role.md and notes.md
fi
cp -R "$src"/. "$APP"/
rm -rf "$src"

mv "$env_clean" "$APP/.env"
chmod 600 "$APP/.env"
# The container runs as uid 1000 and writes notes.md here.
$SUDO chown -R 1000:1000 "$APP/workspace" 2>/dev/null || chmod -R a+rwX "$APP/workspace"

echo "Building and starting (the first build takes a few minutes)..."
cd "$APP"
$COMPOSE up -d --build
sleep 10
$COMPOSE logs --tail 20
echo
echo "Done. If the last lines say 'logged in as ...', message the bot in #homeassistant."
echo "Edit $APP/workspace/role.md to describe your house."
echo "Logs any time: cd $APP && $COMPOSE logs -f"
