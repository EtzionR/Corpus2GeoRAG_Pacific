# Corpus2GeoRAG: Pacific Theater (WWII) 🌊

🚀 **Live app:** [corpus2georagpacific.streamlit.app](https://corpus2georagpacific.streamlit.app/)

The **Streamlit** front end of the project. You ask a question in English, the GeoRAG agent (`../Agent`) answers it from the knowledge graph built from Wikipedia, and the App shows the answer in the chat and its places on a **Folium** map.

## Layout
- **Top bar:** map theme, agent status (connected / mock / unavailable, with the reason), graph statistics, and actions (new conversation, retry the agent).
- **Suggested questions:** with the real agent, four example questions. In mock mode, every question the agent has a recorded reply for (`Agent/geo_agent/mock_fixtures/*.json`); a follow-up like "Tarawa" appears right after its first question.
- **Chat (left) and map (right):** answers, clarification questions, off-topic and error replies, each with confidence, how names were read, result-set totals, source links and warnings. The map draws the agent's GeoJSON: answer places in red, context in grey, places drawn on behalf of another entity with a dashed outline, large sets clustered, and sets across the 180° meridian drawn together.
- **Agentic reasoning (below):** the reply's details, the graph queries the agent ran, and an entity details card. Click a place on the map to open it there.

## How it connects to the agent
- The App imports the agent in-process and uses its runner, `geo_agent.runner.AgentRunner` (one background event loop per process, a time limit per question, one question at a time per conversation). See `Agent/docs/UI_TEAM.md`.
- **Real agent:** needs `OPENROUTER_API_KEY`, in `Agent/.env` locally or in the app's **Secrets** on Streamlit Cloud. Never commit it.
- **Mock mode:** with no key the App switches to the agent's recorded replies on the sample graph and says so in the top bar. No key, network or `.env` is needed. `GEO_AGENT_MOCK=1` forces it.
- While a question runs, the input is disabled, progress is shown, and **Stop** cancels it. Clicking elsewhere meanwhile doesn't lose the answer.
- Agent text and data from the graph are untrusted: they are rendered as Markdown or plain text, never as raw HTML, and map labels are HTML-escaped.

## Quick start 🚀
**Windows:** in the `App` folder, run `setup_local.bat` once (it creates `App\.venv` and installs everything), then `run_local.bat` to start the App. `run_local.bat` allows 240 s per question, because a model request sometimes stalls and is retried.

**Any system:**
1. **Install the dependencies** (the App's and the agent's) in a virtual environment:
   ```bash
   pip install -r requirements.txt
   ```
2. **Optional, for the real agent:** create `Agent/.env` with `OPENROUTER_API_KEY=...` (copy `Agent/.env.example`). Without it the App runs in mock mode. The file is git-ignored; never commit it.
3. **Run the App** from the `App` folder:
   ```bash
   streamlit run app.py
   ```

**Tests** (mock mode, no key needed; they also run on GitHub for every PR that touches `App/` or `Agent/`):
```bash
pip install pytest
pytest tests -q
```

Useful agent settings (environment or Streamlit Secrets): `GEO_AGENT_MODEL`, `GRAPH_PATH` (the graph JSON; default: the sample graph), `GEO_AGENT_RUN_TIMEOUT` (seconds per question, default 120), `GEO_AGENT_MOCK`.

## Notes
- The full draft graph needs about 3 GB of memory and ~30 s to load, which may be more than Streamlit Community Cloud offers. The sample graph and mock mode are small.
- Questions and answers are in English only.
