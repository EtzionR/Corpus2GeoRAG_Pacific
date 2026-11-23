#!/bin/sh
# One-line installer for the Corpus2GeoRAG Pacific app, Mac and Linux (spec R105).
#
#   curl -LsSf https://raw.githubusercontent.com/EtzionR/Corpus2GeoRAG_Pacific/main/Agent/install.sh | sh
#
# Everything goes into one folder (default ~/Corpus2GeoRAG_Pacific): uv, Python 3.13, the packages,
# the code and your key. Nothing is installed system-wide; deleting the folder removes it all.
# Running it again updates the app and keeps your key.
# Needs Git. Settings for testing: GEORAG_DIR, GEORAG_REPO, GEORAG_BRANCH, GEORAG_NO_START=1.
set -eu

REPO="${GEORAG_REPO:-https://github.com/EtzionR/Corpus2GeoRAG_Pacific.git}"
BRANCH="${GEORAG_BRANCH:-main}"
DIR="${GEORAG_DIR:-$HOME/Corpus2GeoRAG_Pacific}"
CODE="$DIR/code"
GRAPH_DIR="$CODE/Agent/data/partof521"
GRAPH="$GRAPH_DIR/graph.selffix.local.json"
ENV_FILE="$CODE/Agent/.env"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mStopped: %s\033[0m\nFix the problem above and run the same command again.\n' "$*" >&2; exit 1; }
sha256() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -c1-64; else sha256sum "$1" | cut -c1-64; fi; }

# --- Git ------------------------------------------------------------------------------------
if ! git --version >/dev/null 2>&1; then
    case "$(uname -s)" in
        Darwin) fail "Git is not installed. A window offering to install it may have opened: click Install, wait until it finishes, then run this command again. (Or run: xcode-select --install)" ;;
        *) fail "Git is not installed. Install it (Ubuntu/Debian: sudo apt install git), then run this command again." ;;
    esac
fi

mkdir -p "$DIR"
# uv and the Python it downloads live inside the folder, not in your home directory.
export UV_UNMANAGED_INSTALL="$DIR/.uv"
export UV_PYTHON_INSTALL_DIR="$DIR/.uv/python"
export UV_CACHE_DIR="$DIR/.uv/cache"
export UV_PYTHON_PREFERENCE=only-managed
UV="$DIR/.uv/uv"

# --- 1. uv ----------------------------------------------------------------------------------
say "1/5  Installing uv (the tool that installs Python and the packages)"
if [ ! -x "$UV" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --quiet || fail "Could not download uv. Check your internet connection."
fi
[ -x "$UV" ] || fail "uv was not installed in $DIR/.uv."

# --- 2. The code: only App/ and Agent/, without Git LFS files --------------------------------
say "2/5  Downloading the app"
export GIT_LFS_SKIP_SMUDGE=1
if [ -d "$CODE/.git" ]; then
    git -C "$CODE" remote set-url origin "$REPO"   # always update from the repository asked for now
    git -C "$CODE" fetch -q --depth 1 origin "$BRANCH" || fail "Could not download the update (branch $BRANCH of $REPO). Check your internet connection."
    # --keep updates the files but stops, without losing anything, if you edited a file the update changes.
    git -C "$CODE" reset -q --keep FETCH_HEAD || fail "You changed files in $CODE that the update also changes. Undo your changes, or delete that folder, and run again."
else
    git clone -q --depth 1 --filter=blob:none --sparse --branch "$BRANCH" "$REPO" "$CODE" || fail "Could not download the app. Check your internet connection."
    git -C "$CODE" sparse-checkout set App Agent || fail "Could not download the app's folders."
fi

# --- 3. The graph: unpack the .gz and check it against SHA256SUMS ----------------------------
say "3/5  Unpacking the knowledge graph"
[ -f "$GRAPH_DIR/graph.selffix.json.gz" ] || fail "The downloaded code has no compressed graph (Agent/data/partof521/graph.selffix.json.gz on branch $BRANCH of $REPO). That version of the app predates the installer: ask the agent developer."
want=$(awk '$2 == "graph.selffix.json" { print $1 }' "$GRAPH_DIR/SHA256SUMS")
[ -n "$want" ] || fail "The graph's checksum is missing from $GRAPH_DIR/SHA256SUMS."
if [ ! -f "$GRAPH" ] || [ "$(sha256 "$GRAPH")" != "$want" ]; then
    gzip -dc "$GRAPH_DIR/graph.selffix.json.gz" > "$GRAPH.tmp" || fail "Could not unpack the graph."
    [ "$(sha256 "$GRAPH.tmp")" = "$want" ] || { rm -f "$GRAPH.tmp"; fail "The unpacked graph is damaged (checksum mismatch). Run again."; }
    mv "$GRAPH.tmp" "$GRAPH"
fi

# --- 4. Python 3.13 and the packages ---------------------------------------------------------
say "4/5  Installing Python 3.13 and the app's packages (the first time takes a few minutes)"
"$UV" venv -q --allow-existing --python 3.13 "$DIR/.venv" || fail "Could not install Python 3.13."
# Prebuilt packages only for cryptography: its newest version has no build for Intel Macs.
"$UV" pip install -q --python "$DIR/.venv" --only-binary cryptography -r "$CODE/App/requirements.txt" \
    || fail "Could not install the app's packages. Check your internet connection."
"$UV" cache prune -q --ci 2>/dev/null || true  # the downloaded packages are installed now; free the space

# --- 5. The OpenRouter key: asked once, saved only in Agent/.env -----------------------------
say "5/5  Your OpenRouter API key"
key=""
if [ -f "$ENV_FILE" ]; then
    key=$(sed -n 's/^OPENROUTER_API_KEY=//p' "$ENV_FILE" | head -n 1)
fi
# `curl | sh` feeds this script on stdin, so the answer is read from the terminal itself. Probe it in a
# subshell first: in dash, a failed `exec` redirection would end the whole script.
if (: </dev/tty) 2>/dev/null; then
    exec 3</dev/tty
    stty -echo <&3 2>/dev/null || true  # before the prompt, so a fast paste is never shown
    if [ -n "$key" ]; then
        printf 'A key is already saved. Press Enter to keep it, or paste a new one: '
    else
        printf 'Paste your key (it stays hidden; press Enter to skip and use demo mode): '
    fi
    IFS= read -r typed <&3 || typed=""
    stty echo <&3 2>/dev/null || true
    exec 3<&-
    printf '\n'
    typed=$(printf '%s' "$typed" | tr -d ' \t\r')
    [ -n "$typed" ] && key="$typed"
else
    printf 'No terminal to ask in; skipping the key.\n'
fi
case "$key" in
    "") printf 'No key for now: the app runs in demo mode, with recorded answers. The launcher asks for a key again each time you start the app.\n' ;;
    sk-or-*) printf 'Key saved.\n' ;;
    *) printf 'Key saved, but it does not look like an OpenRouter key (they start with sk-or-).\n' ;;
esac
umask 077
{
    printf '# Written by install.sh. Keep this file private: whoever has the key can spend its credit.\n'
    printf 'OPENROUTER_API_KEY=%s\n' "$key"
    printf 'GRAPH_PATH=%s\n' "$GRAPH"
    printf 'GEO_AGENT_RUN_TIMEOUT=240\n'
} > "$ENV_FILE"
umask 022

# --- The launcher ---------------------------------------------------------------------------
cat > "$DIR/start.sh" <<'LAUNCHER'
#!/bin/sh
# Starts the Corpus2GeoRAG Pacific app (written by install.sh). Close this window, or press Ctrl+C, to stop it.
HERE=$(cd "$(dirname "$0")" && pwd)
ENV_FILE="$HERE/code/Agent/.env"
# No key saved (it was skipped during the install)? Ask again, hidden. Enter starts demo mode this time.
# Not asked when there's no terminal to ask in, or right after the installer, where it was just skipped.
if [ -t 0 ] && [ "${GEORAG_SKIP_KEY_PROMPT:-}" != 1 ] && ! grep -q '^OPENROUTER_API_KEY=.' "$ENV_FILE" 2>/dev/null; then
    printf 'No OpenRouter key is saved, so the app would run in demo mode (recorded answers only).\n'
    stty -echo 2>/dev/null || true  # before the prompt, so a fast paste is never shown
    printf 'Paste your key to use the real agent (it stays hidden), or press Enter for demo mode: '
    IFS= read -r typed || typed=""
    stty echo 2>/dev/null || true
    printf '\n'
    typed=$(printf '%s' "$typed" | tr -d ' \t\r')
    if [ -n "$typed" ]; then
        umask 077
        touch "$ENV_FILE"
        awk -v k="$typed" '/^OPENROUTER_API_KEY=/ { print "OPENROUTER_API_KEY=" k; done = 1; next } { print }
                           END { if (!done) print "OPENROUTER_API_KEY=" k }' "$ENV_FILE" > "$ENV_FILE.tmp" \
            && mv "$ENV_FILE.tmp" "$ENV_FILE" && printf 'Key saved.\n'
        umask 022
    fi
fi
cd "$HERE/code/App" || exit 1
URL=http://localhost:8501
echo "Starting the app at $URL (the first start takes up to a minute). Keep this window open while you use it."
(
    i=0
    while [ $i -lt 180 ]; do
        if curl -fs -o /dev/null "$URL/_stcore/health"; then
            if command -v open >/dev/null 2>&1; then open "$URL"
            elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
            fi
            break
        fi
        i=$((i + 1)); sleep 1
    done
) &
exec "$HERE/.venv/bin/streamlit" run app.py --server.address localhost --server.port 8501 \
    --server.headless true --browser.gatherUsageStats false
LAUNCHER
chmod +x "$DIR/start.sh"
if [ "$(uname -s)" = Darwin ]; then
    # A .command file opens in Terminal when double-clicked in Finder.
    cp "$DIR/start.sh" "$DIR/Start GeoRAG.command"
    chmod +x "$DIR/Start GeoRAG.command"
fi

say "Done. The app is installed in $DIR"
if [ "$(uname -s)" = Darwin ]; then
    printf 'Next time, double-click "Start GeoRAG.command" in that folder (or run %s/start.sh).\n' "$DIR"
else
    printf 'Next time, run: %s/start.sh\n' "$DIR"
fi
[ "${GEORAG_NO_START:-}" = 1 ] && exit 0
GEORAG_SKIP_KEY_PROMPT=1 exec "$DIR/start.sh"   # the key was just asked for
