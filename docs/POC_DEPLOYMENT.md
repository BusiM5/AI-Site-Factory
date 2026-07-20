# AI Site Factory Free POC Deployment

This proof-of-concept setup keeps the app cheap and demo-friendly:

- Backend API: Render free web service
- Frontend: Vercel or Cloudflare Pages free static hosting
- App registry: SQLite on the backend host for demo state
- Generated site artifact: one GitHub repo per business
- Live site hosting: Netlify builds from the generated GitHub repo
- Approvals/actions: Zendesk webhooks call the Render backend

Important: Render free services do not provide durable local disks. The SQLite file is acceptable for a live demo, but it should not be treated as production storage. GitHub is the durable copy of each generated `index.html`.

For durable campaign data, upgrade the backend to a paid instance and attach the 1 GB disk declared in `render.yaml` at `/var/data`. Keep `PIPELINE_DB_PATH=/var/data/pipeline.db` and `CAMPAIGN_UPLOAD_DIR=/var/data/uploads`. If a Free instance starts with an empty database, the backend can reconstruct managed tickets that reached deploy/cancel/failure state from Zendesk, but untouched intake rows and uploaded import files still require the persistent disk.

## Storage contract

AI Site Factory does not store generated customer sites in the main app repository.

- SQLite stores campaigns, separate email/call lead queues, deferred deployment requests, pipeline runs, duplicate indexes, pending approval HTML, Zendesk ticket links, webhook audits, GitHub export metadata, and Netlify deployment history.
- GitHub stores the generated customer artifact: `index.html` plus `README.md` in a separate repo per business.
- Netlify stores the live site, build ID, deploy ID, and public URL.
- Zendesk stores the operator workflow and outreach thread.

The campaign deployment path is strict GitHub to Netlify and defers model usage:

1. Discover and store a named campaign.
2. Create separate email and call intake records and tagged Zendesk tickets. Do not generate HTML yet.
3. Receive a `deploy_site` webhook from the agent's deploy checkbox.
4. Generate HTML and store the pending artifact in SQLite.
5. Export `index.html` and `README.md` to the lead-owned GitHub repo.
6. Store the GitHub repo, commit SHA, content SHAs, approval ID, canonical lead key, and checksum.
7. Create/update a Netlify Git-linked site and trigger a build from GitHub.
8. Store the build/deploy IDs and live URL, then add the link and outreach template to the same Zendesk ticket.

If another email/call ticket for the same canonical lead requests deployment, the existing artifact or ready deployment is reused.

Direct Netlify zip deployment is only an explicit emergency fallback.

## Backend on Render free

Create a Render web service from this repo.

Use:

```text
Build command:
pip install -r backend/requirements.txt

Start command:
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

The included `render.yaml` contains the same defaults if you deploy as a Render blueprint.

Add these environment variables:

```env
APIFY_API_TOKEN=...
GEMINI_API_KEY=...
GROQ_API_KEY=...
GITHUB_TOKEN=...
GITHUB_OWNER=...
GITHUB_REPO_PRIVATE=false
NETLIFY_AUTH_TOKEN=...
NETLIFY_GITHUB_INSTALLATION_ID=... # only if Netlify requires it for your GitHub integration
ZENDESK_SUBDOMAIN=...
ZENDESK_EMAIL=...
ZENDESK_API_TOKEN=...
ZENDESK_WEBHOOK_SECRET=...
NETLIFY_DEPLOY_POLL_SECONDS=45
```

After deploy, open:

```text
https://YOUR_RENDER_BACKEND.onrender.com/docs
```

Render free services can sleep, so open `/docs` before demos to wake the backend.

## GitHub setup

Create a GitHub token for the account or org that owns generated site repos.

The token must be able to:

- create repositories;
- read repository metadata;
- write repository contents.

Set:

```env
GITHUB_TOKEN=...
GITHUB_OWNER=your-user-or-org
GITHUB_REPO_PRIVATE=false
```

Generated repos are named like:

```text
ai-site-{business-name}-{timestamp}-{lead-hash}
```

Each generated repo receives:

```text
index.html
README.md
```

## Netlify setup

Connect Netlify to the same GitHub account/org used by `GITHUB_OWNER`.

Set:

```env
NETLIFY_AUTH_TOKEN=...
```

If Netlify requires the GitHub installation ID for API-created Git-linked sites, set:

```env
NETLIFY_GITHUB_INSTALLATION_ID=...
```

Approval deploys now use the GitHub-linked Netlify build path by default. If GitHub export metadata is missing, the app blocks deployment and asks you to retry export.

## Frontend deploy

Deploy `frontend` to Vercel or Cloudflare Pages.

Set:

```env
REACT_APP_API_BASE=https://YOUR_RENDER_BACKEND.onrender.com
```

Build command:

```text
npm run build
```

Output directory:

```text
build
```

## Zendesk webhook

The recommended path is the app's **Zendesk setup** screen:

1. Connect with an administrator subdomain, email, and API token.
2. Inspect the instance. This step is read-only and discovers existing fields, forms, views, webhooks, triggers, and brands.
3. Review the 20 preconfigured fields and the separate email/call form membership. Compatible existing string fields are reused; unsafe type conflicts must be resolved first.
4. Select the existing Zendesk brand that will own the managed forms, views, and intake tickets.
5. Optionally select managed views and webhook automation, confirm the disclaimer, and provision.

Provisioning creates fields before forms, then views, then automation. It never deletes Zendesk resources and is safe to rerun. Automation creates one active authenticated webhook and five inactive triggers (email deploy, call deploy, email cancellation, call cancellation, and approved email send) so an administrator can test them before enabling them. Ticket forms require a Zendesk plan that supports multiple forms.

Every backend startup reconciles the stored custom-field IDs against the live field markers before webhook traffic is accepted. Keep the markers in each managed field's agent description; they allow an ephemeral Render instance to replace stale seed IDs safely without renaming or recreating fields.

For a manual setup, use the contract below.

Update the AI Site Factory webhook:

```text
URL:
https://YOUR_RENDER_BACKEND.onrender.com/api/zendesk/webhook

Header name:
x-ai-site-factory-secret

Header value:
the same value as ZENDESK_WEBHOOK_SECRET
```

The deploy trigger should send:

```json
{
  "action": "deploy_site",
  "approvalId": "{{ticket.ticket_field_APPROVAL_ID_FIELD_ID}}",
  "canonicalLeadKey": "{{ticket.ticket_field_CANONICAL_LEAD_KEY_FIELD_ID}}",
  "zendeskTicketId": "{{ticket.id}}",
  "channel": "{{ticket.ticket_field_CONTACT_CHANNEL_FIELD_ID}}",
  "actor": "Zendesk",
  "notes": "Deployment approved from Zendesk ticket {{ticket.id}}"
}
```

The deploy trigger is valid for both `email` and `phone` channel tickets. Configure a second email-only trigger after deployment:

```json
{
  "action": "send_email",
  "approvalId": "{{ticket.ticket_field_APPROVAL_ID_FIELD_ID}}",
  "zendeskTicketId": "{{ticket.id}}",
  "channel": "email",
  "actor": "Zendesk"
}
```

Create separate email and phone cancellation triggers. Each should run when the ticket is below solved, is on the corresponding managed form, has `AI Site Factory - Deploy site` set to false, includes `asf_deployed`, and does not include its channel's cancellation fired tag. The backend writes the checked field and live URL before it adds `asf_deployed`, so the trigger cannot observe the old unchecked field during initial deployment. Send:

```json
{
  "action": "cancel_deployment",
  "approvalId": "{{ticket.ticket_field_APPROVAL_ID_FIELD_ID}}",
  "canonicalLeadKey": "{{ticket.ticket_field_CANONICAL_LEAD_KEY_FIELD_ID}}",
  "zendeskTicketId": "{{ticket.id}}",
  "channel": "{{ticket.ticket_field_CONTACT_CHANNEL_FIELD_ID}}",
  "actor": "Zendesk"
}
```

Cancellation disables the existing Netlify site, clears the live URL on the ticket and local deployment state, and leaves the GitHub repository intact. After a stateless Render restart, the handler can recover the exact site from the managed Zendesk live URL before disabling it. Rechecking the deploy field can re-enable and redeploy it.

For delayed cancellation, the deployed-notification macro must set the ticket to pending and add `asf_customer_notified_deployed` plus `asf_10_day_clock_started`. Zendesk automations run after 240 pending hours and add `asf_10_day_cancellation_due` while unchecking the deploy field. The email automation leaves the public message to the backend: after Netlify is disabled, the backend renders and persists the existing `AI Site Factory::Email::10-day cancellation - notify customer` macro. The phone automation also adds `asf_phone_cancellation_due`; a follow-up trigger waits until the backend adds `asf_deployment_cancelled`, then adds a private note, reopens the ticket, and directs the agent to call the customer with `AI Site Factory::Phone::10-day cancellation - call script`.

Stable form/view tags include `asf_managed`, `asf_form_email_lead`, `asf_form_call_lead`, `asf_channel_email`, `asf_channel_phone`, `asf_source_apify_google_maps`, `asf_source_upload`, `asf_deploy_pending`, `asf_deploy_requested`, `asf_stage_generating`, `asf_artifact_ready`, `asf_repo_ready`, `asf_stage_deploying`, `asf_can_deploy`, `asf_email_send_pending`, `asf_call_pending`, `asf_deployed`, `asf_stage_live`, `asf_deployment_cancelled`, `asf_stage_cancelled`, `asf_cancel_email_fired`, `asf_cancel_phone_fired`, `asf_generation_failed`, `asf_deploy_failed`, and `asf_stage_failed`.

## Demo warm-up checklist

Before the live demo:

1. Open the Render backend `/docs` URL and wait for it to wake.
2. Open the frontend and run the local diagnostics probe.
3. Run a 1–2 lead batch.
4. Confirm each generated site has a GitHub repo and commit SHA.
5. Confirm Zendesk intake tickets were created.
6. Apply the Zendesk deploy macro.
7. Confirm Netlify built from the GitHub repo and returned a live URL.
8. Apply the email and phone macros.
9. Check Operations Hub for Zendesk ticket badges, GitHub artifact metadata, build ID, and live URL.

## Future persistence upgrade

If the POC needs state to survive redeploys/restarts, migrate SQLite to a free Postgres service such as Supabase or Neon. Do not use Google Sheets as the app database; it can be added later as an export/reporting surface.
