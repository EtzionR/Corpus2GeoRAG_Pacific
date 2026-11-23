# Corpus2GeoRAG_Pacific

Ask a question about the WWII Pacific War in plain English and get an answer in a chat, with its places on a map. The answers come from a knowledge graph built from Wikipedia and Wikidata, not from the language model's memory.

Live app: [corpus2georagpacific.streamlit.app](https://corpus2georagpacific.streamlit.app/)

| Folder | What it holds |
|---|---|
| `App/` | The Streamlit app: the chat and the map |
| `Agent/` | The question-answering agent the app calls, and the built graph it reads |
| `Data/`, `GG_Data/`, `Processing/` | The corpus, NER and graph-building pipeline (not needed to run the app) |

---

## Run the app on your computer

Two steps, about 5 minutes the first time. You need an internet connection, about 1 GB of free disk space, and an **OpenRouter API key** for real answers (ask the team for one). Without a key the app still runs, in demo mode, with recorded answers to its suggested questions. You don't need to install Python: the installer brings its own Python 3.13.

**Step 1: install Git (once)**
- **Windows:** install "Git for Windows" from https://git-scm.com/download/win and keep the default options.
- **Mac:** nothing to do now. If Git is missing, step 2 opens a window that offers to install it: click Install, then run step 2 again.
- **Linux (Debian/Ubuntu):** `sudo apt install git`

**Step 2: paste one command**
- **Mac / Linux**, in Terminal:
  ```bash
  curl -LsSf https://raw.githubusercontent.com/EtzionR/Corpus2GeoRAG_Pacific/main/Agent/install.sh | sh
  ```
- **Windows**, in PowerShell or Command Prompt (Start menu → "PowerShell"):
  ```powershell
  powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/EtzionR/Corpus2GeoRAG_Pacific/main/Agent/install.ps1 | iex"
  ```
  The `-ExecutionPolicy Bypass` part lets the installer run on computers that block scripts, for this one run only. It changes no setting.

It installs everything into one folder, `Corpus2GeoRAG_Pacific` in your user folder. It asks for your OpenRouter key (paste it and press Enter; the key stays hidden). If you don't have one yet, press Enter: the app runs in demo mode, and asks for the key again each time you start it. Then it opens the app in your browser at http://localhost:8501. Keep that terminal window open while you use the app; close it to stop the app.

**Next time:** on Windows, double-click **Start GeoRAG** on your desktop. On a Mac, double-click **Start GeoRAG.command** in the `Corpus2GeoRAG_Pacific` folder. On Linux, run `~/Corpus2GeoRAG_Pacific/start.sh`.

**Update to the latest version, or change your key:** run the step 2 command again. It keeps your key unless you paste a new one.

**Remove it:** delete the `Corpus2GeoRAG_Pacific` folder, and on Windows the desktop shortcut. Nothing else was installed on your computer.

**Keep your key private.** It is saved only in `Corpus2GeoRAG_Pacific/code/Agent/.env` on your computer. Whoever has it can spend its credit, so never send that file to anyone.

**If the installer stops,** its last line, in red, starts with "Stopped:" and says what to fix (for example, no internet connection). Fix it and run the same command again: it picks up where it left off. If the message isn't clear, send the window's last lines to the agent developer.

---

## Manual install (step by step)

The same as the one-line install above, one step at a time, for anyone who wants to see or change each step. It needs Git LFS for the graph.

About 15 minutes the first time, with the same needs as above. **Python: 3.13**. You don't need to install Python yourself: `uv`, in step M1, downloads Python 3.13 and keeps it separate from any other Python on your computer.

Use a terminal for every step:
- **Mac:** the Terminal app.
- **Linux:** any terminal.
- **Windows:** PowerShell (Start menu → "PowerShell").

When a step differs between systems, both versions are given.

### M1. Install the tools (once)

You need three tools: **Git**, **Git LFS** (Git's add-on for large files, here the 140 MB graph) and **uv** (installs Python and the app's packages).

**Mac**
```bash
xcode-select --install          # Git; skip if `git --version` already works
brew install git-lfs            # needs Homebrew (https://brew.sh); or the installer from https://git-lfs.com
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Linux (Debian/Ubuntu)**
```bash
sudo apt install git git-lfs
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows**
1. Install **Git for Windows** from https://git-scm.com/download/win (it includes Git LFS; keep the default options).
2. In PowerShell:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

**Then, on every system,** close the terminal, open a new one (so it finds the new tools), and run once:
```bash
git lfs install
```

### M2. Get the code

The repository is large (about 14 GB with the data pipeline's history). These commands download only the two folders the app needs, plus the graph:
```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/EtzionR/Corpus2GeoRAG_Pacific.git
cd Corpus2GeoRAG_Pacific
git sparse-checkout set App Agent
```
These are the same on Mac, Linux and Windows.

Check that the graph arrived. It should be about 134 MB, not a few hundred bytes:
- Mac/Linux: `ls -lh Agent/data/partof521/graph.selffix.json`
- Windows: `dir Agent\data\partof521`

If it's tiny, Git LFS wasn't active during the clone. Run `git lfs pull --include "Agent/data/partof521/*"` and check again.

### M3. Install Python and the app's packages

Run these inside the `Corpus2GeoRAG_Pacific` folder, on any system:
```bash
uv venv --python 3.13
uv pip install --python .venv --only-binary cryptography -r App/requirements.txt
```
- The first command makes a private Python 3.13 environment in the `.venv` folder.
- The second installs the packages into it (a few minutes, about 460 MB).
- `--only-binary cryptography` matters on Intel Macs: the newest version of that package has no ready-made build for them, and the flag makes uv pick the newest one that does. Elsewhere it changes nothing.

### M4. Add the API key

Copy the settings template:
- Mac/Linux: `cp Agent/.env.example Agent/.env`
- Windows: `copy Agent\.env.example Agent\.env`

Open the copy in a text editor:
- Mac: `open -e Agent/.env`
- Linux: `nano Agent/.env`
- Windows: `notepad Agent\.env`

Put your key after the `=` on the first line, and add one line at the end:
```
OPENROUTER_API_KEY=sk-or-...your key...
GEO_AGENT_RUN_TIMEOUT=240
```
Save the file and leave the other lines as they are. `GEO_AGENT_RUN_TIMEOUT=240` gives a question up to 4 minutes: the model provider sometimes stalls a request, and the agent retries it.

`Agent/.env` is ignored by Git. **Never commit it or send it to anyone:** whoever has the key can spend its credit.

### M5. Start the app

**Mac/Linux**
```bash
cd App
../.venv/bin/streamlit run app.py --server.address localhost
```

**Windows**
```powershell
cd App
..\.venv\Scripts\streamlit run app.py --server.address localhost
```

Your browser opens at http://localhost:8501.
- The first start takes up to a minute while the agent loads the graph.
- When everything works, the page shows no status message: ask a question and you get a real answer.
- If the chat says **"Demo mode: recorded answers to the suggested questions."**, the app didn't find a working key or graph and switched to recorded answers. See "If something goes wrong" below.

To stop the app, press `Ctrl+C` in the terminal. `--server.address localhost` keeps the app on your own computer: anyone who could reach it could spend your key.

### Next time

You don't need steps M1 to M4 again. Open a terminal in the `Corpus2GeoRAG_Pacific` folder and repeat step M5.

To update to the team's latest version:
```bash
git pull
uv pip install --python .venv --only-binary cryptography -r App/requirements.txt
```

### If something goes wrong

| What you see | What to do |
|---|---|
| `git`, `git-lfs` or `uv`: "command not found" / "not recognized" | Close the terminal, open a new one, and try again. If it still fails, repeat that tool's install in step M1. |
| The chat says **"Demo mode"** but you added a key | Two possible causes. Check both, then stop the app (`Ctrl+C`) and start it again:<br>1. `Agent/.env` is missing or has no key (step M4). The file must be named exactly `.env`; on Windows, Notepad may save it as `.env.txt`.<br>2. The graph wasn't downloaded (the size check in step M2). Run `git lfs pull --include "Agent/data/partof521/*"`. |
| A red **"Agent unavailable"** box | Its "Reason" line says what failed. Send it to the agent developer. |
| "Port 8501 is already in use" | The app is already running in another terminal. Use that one, or add `--server.port 8502` to the start command. |
| A question takes a long time | Normal now and then: the agent retries stalled model requests. The app's **Stop** button cancels the question. |
| Anything else | Copy the terminal's last lines and send them to the agent developer. |

Demo mode without a key: add `GEO_AGENT_MOCK=1` as a line in `Agent/.env`, or leave the key out. Demo mode needs no internet once installed.
