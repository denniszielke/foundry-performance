# Hypothesis AG-UI

React workflow console for `hosted_hypothesis_agent`. The browser uses the
AG-UI `HttpAgent`; the local Python adapter authenticates with Azure, forwards
`state.workflow` to the Foundry invocations endpoint, and streams AG-UI state
events back to the browser.

```bash
cd src/hypothesis_ag_ui/frontend
npm install
npm run build
cd ../../..
pip install -r src/hypothesis_ag_ui/requirements.txt
python -m src.hypothesis_ag_ui.server
```

Open <http://127.0.0.1:5178>. The server loads the repo `.env` and expects
`AZURE_AI_PROJECT_ENDPOINT` plus `HYPOTHESIS_HOSTED_AGENT_NAME`. Set
`HYPOTHESIS_AGENT_INVOCATIONS_URL` to override the derived endpoint. Azure
authentication uses `DefaultAzureCredential`; run `az login` for local use.

For frontend-only development, run `npm run dev`; Vite proxies `/api` to the
Python server on port 5178.