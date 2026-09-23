# NexusAI Technologies

NexusAI Technologies — AI security technology built for what comes next.

## Render deployment

The repository is configured for a Render Python Web Service.

- Build command: `pip install -r backend/requirements.txt`
- Start command: `uvicorn backend.src.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`
- Public site: `/`
- About: `/about`
- Founder: `/founder`
- Client portal: `/portal/`

Render must receive the application's HTTP server on `0.0.0.0:$PORT`; the included `render.yaml` configures this automatically. citeturn0search0turn0search12

## Environment variables

Set secrets in Render Environment, never in GitHub:

- `NEXUSAI_EDGE_TOKEN` — shared token for Edge Agent API calls
- Future database/API credentials should also be configured as Render environment variables.

Do not commit `.env`.

## Edge Agent

The `edge_agent/` application is designed to run at the customer's site because it needs access to private Hikvision/NVR network addresses. It communicates outbound with the public NexusAI API.

Configure its `NEXUSAI_API_URL` to the Render service URL, for example:

`https://<your-render-service>.onrender.com`

After the custom domain is connected, it can use:

`https://getnexusai.co.za`

## Local development

```bash
pip install -r backend/requirements.txt
uvicorn backend.src.main:app --reload
```

The public site and API are served by the same FastAPI service, so the client portal uses the current origin instead of the old Cloudflare Worker URL.
