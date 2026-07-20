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
- `POST /api/campaigns/intake` creates a named campaign with a dynamic lead target, splits email and call records into separate SQLite tables, and requires tagged Zendesk intake tickets without generating a site.
- `POST /api/campaigns/import` stores a CSV, JSON, or JSONL file and creates a durable, resumable campaign import job. Flexible Apify/Amplifier-style headings are normalized into the managed lead fields.
- `GET /api/campaigns/imports`, `GET /api/campaigns/imports/{job_id}`, and `POST /api/campaigns/imports/{job_id}/process` expose and advance the persisted import queue in small Zendesk-safe chunks. Failed rows can be reset with `POST /api/campaigns/imports/{job_id}/retry`.
- `GET /api/campaigns` and `GET /api/campaigns/{campaign_id}` return campaign funnel, channel, AI-generation, repository, pending, failed, and live metrics.
- `POST /api/campaigns/{campaign_id}/sync-zendesk` retries ticket creation for an already connected and provisioned workspace.
- `POST /api/campaigns/restore-zendesk` previews or idempotently rebuilds local campaign/deployment state from exact `asf_managed` Zendesk tickets without creating or updating Zendesk tickets. By default it restores only leads that entered the deploy, cancellation, or failure workflow; `includePendingIntake` must be explicitly enabled to include untouched intake tickets.
- `GET`, `PUT`, and `DELETE /api/settings/zendesk-connection` expose or manage the Zendesk connection used by the dedicated setup screen. `backend/.env` is the durable source; UI credentials are session-only overrides and tokens are never returned by the API.
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

1. Connect Zendesk, select an existing brand, and provision the managed fields and two forms. Campaign creation, lead queues, and deployments remain locked until this is ready.
2. Every campaign requires a mixed contact set with at least one email lead and one phone lead. Run Apify discovery or upload lead data; the optional metadata generator derives a campaign name and stored industry from the resulting businesses.
3. Zendesk receives separate, tagged email and call tickets. No AI, GitHub, or Netlify work runs yet.
4. An agent ticks the deploy field. The `deploy_site` webhook generates and persists the HTML, exports the repository, deploys Netlify, and writes the live URL back to the same ticket. Transient GitHub 429/5xx and connection failures are retried with backoff; a repository created before a failed file upload is recovered on the next attempt instead of duplicated, and the saved HTML is reused without another model call.
5. A second channel for the same canonical lead reuses the existing artifact or live deployment.
6. Email tickets can fire the separate `send_email` webhook after the agent reviews the generated template. Call tickets retain the live link for the agent's call workflow.
7. If an agent later unticks the deploy field on a live ticket, the `cancel_deployment` webhook disables the Netlify site, clears the live URL, and retains the GitHub artifact. Deployment completion writes the checked field and live URL before adding `asf_deployed`, and preserves the checkbox-backed `asf_deploy_requested` tag in the final live tag set. This prevents lifecycle tag cleanup from unchecking the field and matching the cancellation trigger during the same update. If Render has restarted without its local registry, the backend recovers the exact Netlify site from the managed ticket's live URL first. Ticking the field again can re-enable and redeploy the same lead-owned site.
8. The email and phone 10-day automations run only after the deployed-customer notification has placed the ticket in pending status. At 240 hours they uncheck the deploy field and add `asf_10_day_cancellation_due`. The backend undeploys first; for email tickets it then renders and applies `AI Site Factory::Email::10-day cancellation - notify customer`. For phone tickets, a follow-up trigger waits for `asf_deployment_cancelled`, reopens the ticket, and adds a private note directing the agent to call and apply `AI Site Factory::Phone::10-day cancellation - call script`.

Campaign persistence uses `campaigns`, `campaign_email_leads`, `campaign_call_leads`, and `campaign_deployments`. Uploaded files add `campaign_import_jobs` and per-row `campaign_import_items`; the source file is retained while the job is pending and removed after every row completes. The deployment row stores its campaign, approval, canonical lead, and channel so graphs can distinguish pending requests, AI generations, repositories created, failures, and live sites.

On Render, durable SQLite storage requires the paid Starter instance and the 1 GB persistent disk declared in `render.yaml`. The database path is `/var/data/pipeline.db` and uploaded campaign files use `/var/data/uploads`. As a recovery safety net, an empty Render database rebuilds the managed Zendesk tickets that have entered the deploy/cancel workflow. A comma-separated `ZENDESK_CAMPAIGN_RECOVERY_TICKET_IDS` allowlist can retain selected untouched demo tickets across Free-tier restarts; `render.yaml` contains the current 20-record demo set. Set `ENABLE_ZENDESK_CAMPAIGN_RECOVERY_ON_STARTUP=false` to disable startup recovery. This recovery is not a substitute for the persistent disk because arbitrary intake campaigns and import files do not have a complete external copy.

The Zendesk setup wizard requires one existing brand selected from the live instance inventory. Both forms and managed views are scoped to that brand, and its ID is added to every intake ticket. Custom field IDs are discovered and saved rather than typed into the UI. Existing compatible text fields are adopted without changing their type, while unsafe mismatches block provisioning before any writes. Optional webhook triggers are created inactive for review and testing.

On application startup, saved Zendesk field IDs are reconciled against the live fields' stable `[AI Site Factory key=...]` markers. This prevents an ephemeral Render restart from restoring stale IDs and causing otherwise valid webhook tickets to fail identity validation.

Ticket lifecycle tags are stable automation contracts. Intake uses `asf_managed`, source (`asf_source_apify_google_maps` or `asf_source_upload`), channel/form, and `asf_deploy_pending` tags. Deployment progresses through `asf_deploy_requested`, `asf_stage_generating`, `asf_artifact_ready`, `asf_repo_ready`, `asf_stage_deploying`, and finally `asf_deployed` plus `asf_stage_live`. The delayed cancellation handoff uses `asf_customer_notified_deployed`, `asf_10_day_clock_started`, `asf_10_day_cancellation_due`, `asf_10_day_cancellation_sent`, and, for calls, `asf_phone_cancellation_due` plus `asf_phone_cancellation_note_added`. Completed cancellation uses `asf_deployment_cancelled` plus `asf_stage_cancelled`; failures use `asf_generation_failed` or `asf_deploy_failed` with `asf_stage_failed`.

On backend startup, `backfill_legacy_campaign_data()` imports pre-campaign discovery batches, pipeline runs, approvals, Zendesk tickets, GitHub exports, and deployment history into these tables. Deterministic legacy IDs and `INSERT OR IGNORE` make the migration safe to run repeatedly. `POST /api/campaigns/backfill` can trigger the same idempotent import manually.

## Environment
Configure provider credentials in `backend/.env` for local development and in Render environment variables for production. Use `backend/.env.example` as the variable-name reference; it contains no usable secrets.

### Administrator login

The dashboard remains available while administrator authentication is unconfigured. Once the following backend variables are present, all dashboard API routes require the secure login session. Zendesk's authenticated webhook and the protected maintenance endpoint keep their existing independent authentication.

1. Run `python scripts/hash_admin_password.py` from the repository root.
2. Enter and confirm a password containing at least 12 characters. The script prints a PBKDF2 hash and never writes the password to disk.
3. Set `ADMIN_USERNAME` to the chosen username, such as `admin`.
4. Set `ADMIN_PASSWORD_HASH` to the complete generated value beginning with `pbkdf2_sha256$`.
5. Set `ADMIN_SESSION_SECRET` to a separate random value containing at least 32 characters.
6. Optionally set `ADMIN_SESSION_MAX_AGE_SECONDS`; the default is eight hours (`28800`).
7. Restart the backend. The frontend detects the protected backend automatically and displays the login screen.

Do not add the plain password to source code, Vercel variables, browser storage, or any `VITE_` variable. `ADMIN_PASSWORD` is supported only as a temporary server-side fallback; `ADMIN_PASSWORD_HASH` is the recommended production setting.

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

  

  
