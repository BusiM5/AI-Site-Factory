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
- `GET /api/presets` returns the available Google Maps business presets.
- `GET /api/templates` returns the landing-page template metadata.
- `POST /api/leads/discover` searches Apify Google Maps across all nine South African provinces, normalizes leads, stores canonical lead keys, and skips previously seen leads.
- `POST /api/campaigns/intake` creates a named campaign, discovers up to 10,000 requested leads, splits email and call records into separate SQLite tables, and optionally creates tagged Zendesk intake tickets without generating a site.
- `GET /api/campaigns` and `GET /api/campaigns/{campaign_id}` return campaign funnel, channel, AI-generation, repository, pending, failed, and live metrics.
- `POST /api/campaigns/{campaign_id}/sync-zendesk` sends locally saved campaign records to Zendesk after a connection is configured.
- `GET`, `PUT`, and `DELETE /api/settings/zendesk-connection` manage the optional runtime Zendesk connection used by the dedicated setup screen. Tokens are never returned by the API.
- `GET /api/settings/zendesk-setup` returns the preconfigured field, form, view, tag, and automation blueprint without contacting Zendesk.
- `POST /api/settings/zendesk-setup/inspect` performs a read-only instance inventory and reports exact matches, compatible existing fields, missing resources, type conflicts, available brands, and plan-dependent capabilities.
- `POST /api/settings/zendesk-setup/provision` requires explicit confirmation and provisions in dependency order: ticket fields, two channel forms, optional views, then an optional authenticated webhook with inactive triggers. Reruns reuse or reconcile saved/exact-name resources rather than duplicating them.
- `POST /api/pipeline/run` generates final HTML through Gemini -> Groq -> Gemini and stores the result as `PENDING_APPROVAL`.
- `GET /api/pipeline/runs` and `GET /api/pipeline/runs/{pipeline_id}` expose recent run status and step history.
- `POST /api/leads/{canonical_lead_key}/owner` updates lead ownership metadata without adding auth.
- `GET /api/approvals` lists generated sites waiting for review.
- `GET /api/approvals/{approval_id}` returns approval details and optional preview HTML.
- `POST /api/approvals/{approval_id}/approve` deploys or redeploys the lead-owned Netlify site, records deployment history, and creates the current Zendesk outreach ticket.
- `POST /api/approvals/{approval_id}/reject` rejects a generated site without deployment.
- `POST /api/approvals/{approval_id}/regenerate` creates a fresh generated page for manual approval.
- `GET /api/reporting/summary` and `GET /api/deployments/history` power the dashboard metrics and audit views.

## Campaign workflow

1. Name a campaign and choose email leads, call leads, or both.
2. Apify discovery stores canonical lead data and creates channel-specific intake records.
3. Zendesk receives separate, tagged email and call tickets. No AI, GitHub, or Netlify work runs yet.
4. An agent ticks the deploy field. The `deploy_site` webhook generates the HTML, exports the repository, deploys Netlify, and writes the live URL back to the same ticket.
5. A second channel for the same canonical lead reuses the existing artifact or live deployment.
6. Email tickets can fire the separate `send_email` webhook after the agent reviews the generated template. Call tickets retain the live link for the agent's call workflow.

Campaign persistence uses `campaigns`, `campaign_email_leads`, `campaign_call_leads`, and `campaign_deployments`. The deployment row stores its campaign, approval, canonical lead, and channel so graphs can distinguish pending requests, AI generations, repositories created, failures, and live sites.

The Zendesk setup wizard uses all brands by default. An administrator can instead restrict both forms to one discovered brand. Provisioned form and brand IDs are persisted locally and automatically added to new intake tickets; custom field IDs are discovered and saved rather than typed into the UI. Existing compatible text fields are adopted without changing their type, while unsafe mismatches block provisioning before any writes. Optional webhook triggers are created inactive for review and testing.

On backend startup, `backfill_legacy_campaign_data()` imports pre-campaign discovery batches, pipeline runs, approvals, Zendesk tickets, GitHub exports, and deployment history into these tables. Deterministic legacy IDs and `INSERT OR IGNORE` make the migration safe to run repeatedly. `POST /api/campaigns/backfill` can trigger the same idempotent import manually.

## Environment
Configure provider credentials in `backend/.env`. The example env file is intentionally not included because this project uses real provider tokens locally; keep secrets out of source control and rotate any credential that was shared outside a secret manager.

## Vercel frontend deployment

The frontend is a Vite application and its production output is `dist`, not Create React App's legacy `build` directory. The repository includes both `frontend/vercel.json` (when the Vercel Root Directory is `frontend`) and a root `vercel.json` (when the project uses the repository root). Both configurations set the correct build/output paths and rewrite client-side routes to `index.html`.

For the existing Vercel project, keep the Root Directory set to `frontend` and redeploy. The checked-in configuration overrides the old `build` Output Directory setting.
## CRM Tracking
- Zendesk API

## Email Sending
-

## File Handling
- Pandas for CSV cleaning

## Hosting
- Render/Railway
- AWS later

  

  
