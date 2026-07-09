# AI-Site-Factory-and-Outreach-Pipeline
This project is an end-to-end automation system that collects business data, cleans and structures it, and uses AI to generate high-quality website content and personalized outreach messages. The system streamlines the entire workflow from raw data ingestion to publish-ready content and communication.

## Tech stack
### Frontend
- React
- Tailwind CSS
- Vercel (Deployment)
- https://ai-site-factory-frontend.vercel.app/ 
### Backend
- Python(FastAPI)
- Render(Deployment)
- https://ai-site-factory-backend.onrender.com/docs
### Database
- SQLite-backed pipeline registry (`PIPELINE_DB_PATH`, defaults to `backend/data/pipeline.db`)

## AI Layer
- Google GeminiAPI(Vertex AI)

## Deployment
- Netlify

## Free POC deployment
For the live proof-of-concept deployment checklist, strict GitHub -> Netlify artifact flow, Render backend setup, frontend env setup, and Zendesk webhook steps, see [docs/POC_DEPLOYMENT.md](docs/POC_DEPLOYMENT.md).

## Lead Pipeline API
- `GET /api/presets` returns the five Google Maps business examples.
- `GET /api/templates` returns the three landing-page templates.
- `POST /api/leads/discover` searches Apify Google Maps across all nine South African provinces, normalizes leads, stores canonical lead keys, and skips previously seen leads.
- `POST /api/pipeline/run` generates final HTML through Gemini -> Groq -> Gemini and stores the result as `PENDING_APPROVAL`.
- `GET /api/pipeline/runs` and `GET /api/pipeline/runs/{pipeline_id}` expose recent run status and step history.
- `POST /api/leads/{canonical_lead_key}/owner` updates lead ownership metadata without adding auth.
- `GET /api/approvals` lists generated sites waiting for review.
- `GET /api/approvals/{approval_id}` returns approval details and optional preview HTML.
- `POST /api/approvals/{approval_id}/approve` deploys or redeploys the lead-owned Netlify site, records deployment history, and creates the current Zendesk outreach ticket.
- `POST /api/approvals/{approval_id}/reject` rejects a generated site without deployment.
- `POST /api/approvals/{approval_id}/regenerate` creates a fresh generated page for manual approval.
- `GET /api/reporting/summary` and `GET /api/deployments/history` power the dashboard metrics and audit views.

## Environment
Configure provider credentials in `backend/.env`. The example env file is intentionally not included because this project uses real provider tokens locally; keep secrets out of source control and rotate any credential that was shared outside a secret manager.
## CRM Tracking
- Zendesk API

## Email Sending
-

## File Handling
- Pandas for CSV cleaning

## Hosting
- Render/Railway
- AWS later

  

  
