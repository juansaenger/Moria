#!/bin/sh
# Deploy watcher. Runs from cron on the server, OUTSIDE the bot.
#
# The bot cannot call this and cannot edit it: deploy/ is on its forbidden list,
# so a pull request it writes can never change what ships or how. What ships is
# decided by a human merging a pull request; this just notices and acts on it.
#
# On a new commit to the tracked branch it: pulls, runs the tests, rebuilds,
# waits for the bot to report that it logged in, and puts the previous image
# back if any of that fails. Then it says what happened in Discord.
#
# Install:
#   cp deploy/watch.sh /DATA/AppData/homebot/deploy/watch.sh && chmod +x it
#   crontab -e  ->  */5 * * * * /DATA/AppData/homebot/deploy/watch.sh >/dev/null 2>&1
# Updating the watcher itself is a deliberate manual copy, by design.
set -eu

HOME_DIR=/DATA/AppData/homebot
REPO="$HOME_DIR/repo"
APP="$HOME_DIR/app"
STATE="$HOME_DIR/deployed.sha"
LOG="$HOME_DIR/deploy.log"
LOCK="$HOME_DIR/deploy.lock"
HEALTH_WAIT=120
TEST_IMAGE=python:3.12-slim

export PATH="$HOME_DIR/bin:$PATH"
export DOCKER_CONFIG="$HOME_DIR/.docker"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

discord() {
    token=$(grep '^DISCORD_BOT_TOKEN=' "$HOME_DIR/homebot.env" | cut -d= -f2- | tr -d '\r')
    channel=$(grep '^DISCORD_CHANNEL_ID=' "$HOME_DIR/homebot.env" | cut -d= -f2- | tr -d '\r')
    [ -n "$token" ] && [ -n "$channel" ] || return 0
    curl -s -o /dev/null -X POST \
        -H "Authorization: Bot $token" \
        -H "Content-Type: application/json" \
        --data "$(printf '{"content":%s}' "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")" \
        "https://discord.com/api/v10/channels/$channel/messages" || true
}

# One at a time. A second cron tick during a build must not start another.
exec 9>"$LOCK"
flock -n 9 || exit 0

BRANCH=$(grep '^GITHUB_BASE_BRANCH=' "$HOME_DIR/homebot.env" | cut -d= -f2- | tr -d '\r')
BRANCH=${BRANCH:-claude/sharp-allen-t1cp76}

git -C "$REPO" fetch --quiet origin "$BRANCH" || { say "fetch failed"; exit 0; }
TARGET=$(git -C "$REPO" rev-parse "origin/$BRANCH")
CURRENT=$(cat "$STATE" 2>/dev/null || echo none)

[ "$TARGET" = "$CURRENT" ] && exit 0

SUBJECT=$(git -C "$REPO" log -1 --pretty=%s "origin/$BRANCH")
SHORT=$(git -C "$REPO" rev-parse --short "origin/$BRANCH")
say "new commit $SHORT: $SUBJECT"

git -C "$REPO" reset --quiet --hard "origin/$BRANCH"

# Tests first, in a throwaway container, so a broken merge never reaches the house.
say "running tests"
# Copy into the container before installing: the mount stays read-only, so a
# test run can never write to the checkout the next deploy reads from.
if ! docker run --rm -v "$REPO:/src:ro" "$TEST_IMAGE" \
        sh -c 'cp -r /src /build && cd /build && pip install -q ".[dev]" && python -m pytest -q' \
        >> "$LOG" 2>&1; then
    say "TESTS FAILED, not deploying"
    discord "Did not deploy \`$SHORT\` ($SUBJECT): the tests failed. Nothing changed; I am still on the previous build."
    echo "$TARGET" > "$STATE.failed"
    exit 0
fi

PREVIOUS=$(docker inspect homebot --format '{{.Image}}' 2>/dev/null || echo "")

say "syncing and rebuilding"
rsync -a --delete \
    --exclude='.git' --exclude='.env' --exclude='workspace' \
    --exclude='bin' --exclude='.docker' --exclude='homebot.env' \
    "$REPO/" "$APP/"
cd "$APP"
if ! docker compose up -d --build --force-recreate >> "$LOG" 2>&1; then
    say "BUILD FAILED"
    discord "Deploy of \`$SHORT\` failed while building. Still on the previous build."
    exit 0
fi

# Healthy means the bot actually reached Discord, not merely that a process started.
say "waiting for the bot to log in"
healthy=0
waited=0
while [ "$waited" -lt "$HEALTH_WAIT" ]; do
    if docker compose logs --since 3m 2>/dev/null | grep -q "logged in as"; then
        healthy=1
        break
    fi
    sleep 5
    waited=$((waited + 5))
done

if [ "$healthy" = "1" ]; then
    echo "$TARGET" > "$STATE"
    say "deployed $SHORT"
    discord "Deployed \`$SHORT\` - $SUBJECT"
    exit 0
fi

say "UNHEALTHY, rolling back to $PREVIOUS"
if [ -n "$PREVIOUS" ]; then
    docker tag "$PREVIOUS" app-homebot:latest >> "$LOG" 2>&1 || true
    docker compose up -d --force-recreate --no-build >> "$LOG" 2>&1 || true
    discord "Deploy of \`$SHORT\` did not come up, so I put the previous build back. Check \`deploy.log\` on the server."
else
    discord "Deploy of \`$SHORT\` did not come up and there was no previous image to restore. The bot may be down."
fi
exit 0
