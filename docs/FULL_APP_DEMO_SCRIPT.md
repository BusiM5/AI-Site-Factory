# AI Site Factory — Full Application Demo Script

**Recommended duration:** 30–40 minutes  
**Short version:** Skip the deep technical callouts and cancellation test for a 15–20 minute presentation.  
**Audience:** Business stakeholders, Zendesk administrators, agents, technical reviewers, and implementation partners  
**Demo objective:** Show the complete path from a qualified lead to an agent-approved live site, channel follow-up, reporting, and controlled cancellation.

## 1. The story of the demo

The simplest non-technical description is:

> AI Site Factory finds public businesses that do not have a website, creates a structured lead campaign, and sends those leads into separate Zendesk email and phone queues. It does not create a website immediately. A Zendesk agent chooses which leads deserve a site. Only then does the system personalize a webpage, store it in GitHub, deploy it to Netlify, and return the live link to the same Zendesk ticket. If the customer does not respond after the configured 10-day period, the site can be disabled and the correct customer or agent message is triggered.

The main business value is not “AI can write HTML.” The value is a governed workflow where discovery, agent approval, customer conversation, generated artifact, hosting, and cancellation remain traceable.

## 2. Roles used in the demo

| Role | What they see/do |
|---|---|
| AI Site Factory administrator | Login, setup, campaigns, queues, reporting, settings, and API safety. |
| Zendesk administrator | Fields, forms, views, webhook, triggers, automations, and macros. |
| Zendesk agent | Lead ticket, deploy approval, email/call workflow, and cancellation handoff. |
| Customer/prospect | Receives an email or call with a personalized live preview link. |
| Technical reviewer | Observes IDs, webhook actions, pipeline states, repository, build, recovery, and security controls. |

## 3. Demo safety rules

Use a dedicated test campaign and designated test Zendesk tickets. The demo can create billable or externally visible resources.

Before presenting:

- Do not expose API keys, passwords, webhook secrets, tokens, cookies, or full environment-variable values.
- Do not provision a production Zendesk instance live unless the owner explicitly intends those changes.
- Do not use a real customer email address for a public test message.
- Do not check Deploy site on multiple records to “see what happens.” Each first-time deployment can consume AI, GitHub, and Netlify capacity.
- Do not demonstrate cancellation on a customer’s real active site.
- Use public or synthetic business data with an approved test contact route.
- If using a temporary short cancellation automation, remove/disable it and restore the 240-hour condition immediately after the test.

## 4. Pre-demo checklist

Complete this 30–60 minutes before the session.

### Infrastructure

- [ ] Vercel frontend opens successfully.
- [ ] Render `/api/health` returns `READY`.
- [ ] Administrator username/password is confirmed privately.
- [ ] Render persistent-storage configuration is known.
- [ ] API Safety Center shows expected providers configured.
- [ ] Current Gemini text model is available; do not rely on shut-down `gemini-2.0-flash`.
- [ ] GitHub token/owner and Netlify GitHub installation are valid.
- [ ] Netlify has enough production-deploy credits.

### Zendesk

- [ ] AI Site Factory brand assignment is correct.
- [ ] Email and phone forms exist.
- [ ] Managed fields have current IDs.
- [ ] Ticket-actions webhook is active and uses the correct HTTPS endpoint/secret header.
- [ ] Intended deploy/cancel/email triggers are active.
- [ ] Deployed notification and 10-day macros/automations exist.
- [ ] A test agent is assigned or ready to assign the demo ticket.

### Data and fallback preparation

- [ ] Prepare a small mixed file with at least one email route and one phone route, no website values, and approved contact data.
- [ ] Keep an existing undeployed test campaign available if live Apify discovery returns no email leads.
- [ ] Keep one existing deployed test ticket available to show final state.
- [ ] Open the relevant Zendesk, GitHub, and Netlify tabs before presenting, but keep sensitive screens hidden.
- [ ] Record the expected test ticket ID, approval ID, repository, and live URL privately.

## 5. Presentation structure

| Segment | Time | Primary audience |
|---|---:|---|
| 1. Business problem and architecture | 3 min | Everyone |
| 2. Login, branding, and security | 2 min | Everyone/technical |
| 3. Overview dashboard | 4 min | Business/operations |
| 4. Settings, accessibility, and API safety | 3 min | Admin/technical |
| 5. Zendesk setup | 5 min | Zendesk admin/technical |
| 6. Create or import a mixed campaign | 5 min | Business/admin |
| 7. Lead workspace and Zendesk ticket | 4 min | Agent/operations |
| 8. Deploy one website | 7 min | Everyone |
| 9. Generated site quality | 3 min | Business/design |
| 10. Email and phone follow-up | 3 min | Agents |
| 11. Cancellation and recovery | 4 min | Operations/technical |
| 12. Reporting, cost control, and wrap-up | 3 min | Stakeholders |

## 6. Detailed demo script

### Segment 1 — Business problem and architecture

**Screen:** A simple architecture slide or the Overview hero.

**Say:**

> Many local businesses are discoverable through public listings but do not have a website. A raw scraper can find them, and an AI model can generate a page, but that alone does not create a controlled business process. AI Site Factory joins the entire process together. Apify supplies lead data, Zendesk owns agent approval and customer communication, Groq and Gemini personalize the site only when requested, GitHub stores the artifact, and Netlify hosts the live result.

> The important cost and governance decision is that discovering a lead does not automatically generate anything. The agent decides which individual lead deserves a deployed site.

**Technical callout:**

- Vercel hosts the React/Vite administration interface.
- Render hosts the FastAPI API and SQLite workflow registry.
- Zendesk calls an authenticated backend webhook.
- The canonical lead key deduplicates businesses across channels/campaigns.
- GitHub and Netlify IDs are persisted for audit/recovery.

**Expected audience takeaway:** This is a workflow/orchestration product, not just an HTML generator.

### Segment 2 — Login, branding, and security

**Screen:** Administrator login.

**Action:** Sign in with the prepared administrator credential. Do not show the password.

**Say:**

> The dashboard is protected by a server-verified administrator login. The password is not stored in the frontend or browser. The backend stores a salted PBKDF2 hash and issues an HTTP-only signed session cookie.

> This initial version intentionally has one administrator. Multiple users and role-based permissions are a future extension.

**Technical callout:**

- Five failed attempts per IP/username in 15 minutes are rate-limited.
- Default session duration is eight hours.
- Changing the password hash invalidates existing sessions.
- Zendesk webhooks remain separately authenticated and do not depend on a browser login.

**Point out:** Logo, technical background animation, password visibility control, and secure-login wording.

### Segment 3 — Overview dashboard

**Screen:** `/overview`.

**Say:**

> This is the campaign command centre. The top explains the operating sequence: lead discovery, Zendesk workflow, AI plus GitHub artifact creation, and Netlify deployment.

Walk through the cards:

1. **Campaigns** — named lead-generation activities.
2. **Lead records** — combined email and call queues.
3. **Live deployments** — Netlify sites currently ready.
4. **AI generations** — logical site generations, including restored history.
5. **Repos created** — durable site artifacts in GitHub.
6. **Pending** — records waiting for agent action or completion.

Then explain the graphs:

- **Deployment health:** live, pending, and failed.
- **Campaign funnel:** discovered → channel records → Zendesk → deploy requested → AI generated → repo → live.
- **Campaign comparison:** which campaign is producing deployments and which still has opportunity.

**Technical callout:**

- Metrics are derived from campaign channel/deployment tables rather than browser-only counters.
- Numbers and charts animate, but the accessibility setting and operating-system preference can disable motion.
- The page refreshes periodically and can be refreshed manually.

**Optional line:**

> Notice that a high lead count with a low AI-generation count is intentional. It means the system is saving model and hosting resources until an agent selects a lead.

### Segment 4 — Settings, themes, accessibility, and API safety

**Screen:** `/settings`.

**Action:** Demonstrate Light, Dark, and System theme. Toggle High contrast and Reduce motion, then reset if desired.

**Say:**

> Settings cover both usability and operational safety. Theme and accessibility preferences are functional, not decorative mockups. Motion can be stopped, contrast strengthened, focus made more visible, and text scaled.

> The API Safety Center reports whether the backend has each required provider variable without returning the secret itself. A deliberate probe can validate one provider connection.

**Technical callout:**

- Preferences are persisted locally for the browser.
- System dark mode and `prefers-reduced-motion` are respected.
- Provider readiness includes Apify, Gemini, Groq, GitHub, Netlify, and Zendesk.
- Probes should be run before a demo; they may call live providers but are designed as minimal readiness checks.

**Do not:** Open Render environment values or expose token text during the presentation.

### Segment 5 — Zendesk setup

**Screen:** `/zendesk`, then Zendesk Admin Center in a prepared tab.

**Say:**

> Instead of asking an administrator to manually type twenty field IDs, the app connects to the instance, performs a read-only inventory, and builds a setup plan. The administrator selects an existing brand and authorizes changes only after reviewing the plan.

**Walk through:**

1. Connection inputs: subdomain, administrator username/email, API token.
2. **Inspect instance**: read-only matching and conflict check.
3. Existing brand selection.
4. Field/resource table with discovered IDs.
5. Separate email/call form memberships.
6. Stable tags used for views, reporting, and trigger guards.
7. Provision confirmation disclaimer.

**Say:**

> Provisioning is sequential because forms cannot reference fields that do not exist. The app creates or reconciles fields first, forms second, views third, and optional webhook automation last. It never deletes resources. Newly staged triggers are inactive so a Zendesk administrator can inspect and test them before activation.

**Technical callout:**

- Stable `[AI Site Factory key=...]` descriptions allow IDs to be rediscovered after a backend restart.
- Compatible existing text fields can be adopted.
- Unsafe type conflicts stop the run before writes.
- Forms are assigned to the selected existing brand.

**In Zendesk, show:**

- Email Lead form.
- Call Lead form.
- Deploy checkbox, status, source, canonical/approval IDs, and live URL.
- Ticket-actions webhook endpoint and active status—do not expose the secret value.
- Trigger conditions at a high level.

### Segment 6 — Create or import a mixed campaign

**Screen:** `/campaigns`.

#### Option A — Find leads

**Action:** Show the fields without necessarily running a live large search.

**Say:**

> Preset cards are shortcuts, but the administrator can enter any campaign name, industry, location, and search intent. The optional metadata setting derives a campaign name and stored industry from the returned businesses.

> Both channels are mandatory. The backend will create the campaign only if the final no-website set contains at least one email route and one phone route. It never invents missing contact information.

Explain filters:

- existing website → excluded;
- no phone and no email → excluded;
- duplicate/canonical existing lead → excluded;
- qualifying lead → considered for mixed selection.

#### Option B — Upload lead data (recommended for a predictable live demo)

**Action:** Select the prepared mixed CSV/JSON file, show automatic/manual campaign metadata and chunk size, then submit only if approved test data is being used.

**Say:**

> Upload uses the same business rules and Zendesk-first workflow. The job persists row state and processes in small chunks so a large file does not overwhelm Zendesk. Failed rows can be retried without repeating successful rows.

**Expected success:** A notice confirms the campaign and number of channel records; no site has been generated.

**Expected controlled failure:** A phone-only or email-only file is blocked with a clear mixed-channel explanation before tickets are created.

### Segment 7 — Lead workspace and Zendesk intake ticket

**Screen:** `/leads`, select the demo campaign.

**Say:**

> This workspace separates the email and call queues because the data and agent actions are different. Email records have an address and an approved-send action. Phone records have a number and call-status workflow. Both can request a site.

Point out:

- campaign, industry, location, and Apify batch context;
- Email Leads and Call Leads tabs/counts;
- business/contact, source listing, Zendesk ticket, deploy request, and status;
- direct links to the public listing and Zendesk ticket.

Open one prepared Zendesk ticket.

**Say:**

> The requester is an end user named after the business, not the API agent. This allows agents to work with a recognizable business identity even when the prospect does not yet have a Zendesk account or usable email address.

Show:

- business-named requester;
- private first note;
- campaign and business fields;
- correct email/call form;
- correct contact field;
- source URL;
- deploy checkbox unchecked;
- live URL blank;
- awaiting deployment status/tags.

**Technical callout:**

- Ticket `external_id` and an API idempotency key prevent duplicate tickets.
- Returned brand/form/custom fields/requester are verified after creation.
- The approval exists but HTML is still empty/awaiting deployment.

### Segment 8 — Deploy one personalized website

**Screen:** The designated undeployed Zendesk test ticket.

**Action:** Assign the ticket if required. Check **AI Site Factory - Deploy site** and submit the ticket once.

**Say before submitting:**

> This checkbox is the financial and governance boundary. Until this moment there was no AI site, repository, or Netlify deployment for this lead.

After submitting, narrate the states:

1. **Deployment requested / generating**
   - Groq compacts the supplied public lead information into a structured business brief.
   - Gemini produces a complete single-file landing page.
2. **Artifact ready**
   - HTML is saved in SQLite before external repository work.
   - GitHub creates or recovers the lead-owned repository and writes `README.md` plus `index.html`.
3. **Deploying**
   - Netlify creates/reuses a Git-linked site and runs a production build.
4. **Live**
   - Site/build/deploy IDs and URL are stored.
   - Zendesk receives the checked field, live URL, private lifecycle note, and live tags.

**Technical callout:**

- A deployment claim lease prevents repeated webhook delivery from producing duplicates.
- If GitHub fails after HTML is generated, retry uses the saved HTML instead of paying for another model call.
- A second email/phone ticket for the same canonical business can reuse the live site.
- A partial GitHub repo is recovered instead of creating a new one.

Open the GitHub repository and point out:

- unique business repo name;
- `README.md` audit metadata;
- `index.html`;
- commit history/commit SHA.

Open Netlify and point out:

- linked GitHub repository;
- production build/deploy;
- public URL.

Return to Zendesk and show the live URL and final lifecycle tags.

### Segment 9 — Generated website quality

**Screen:** Open the live Netlify site on desktop and a narrow/mobile viewport if convenient.

**Say:**

> The page is not a generic template with the name swapped. It uses the business’s supplied category, location, contact route, image, and grounded listing data to create a suitable theme, caption, service presentation, proof points, and calls to action.

Show:

- exact public business main image when available;
- industry-appropriate colors;
- business-specific tagline;
- four distinct personalized services;
- location/address/source/rating/review information when supplied;
- working Call, Email, View services, and section links;
- mobile layout;
- generated-site color widget;
- no fabricated award, price, employee, or guarantee claims.

**Technical callout:**

- If the main image is absent, a business-specific inline SVG is produced without a stock-photo dependency.
- The backend post-processes non-working CTA targets.
- Insufficiently personalized model output is replaced with the deterministic personalized renderer.
- Bootstrap and animation/style assets are validated.

### Segment 10 — Email and phone agent workflows

#### Email ticket

**Action:** Show the deployed email ticket and **Send approved email** field. Apply the deployed-site/customer macro or trigger the approved email only with a designated test recipient.

**Say:**

> Deployment does not automatically send an email. The agent remains responsible for reviewing the message and authorizing the public response. The content states where the business was found and includes the preview link.

#### Phone ticket

**Action:** Show the phone form, live URL, call-status choices, and call macro/script.

**Say:**

> A phone-only lead should not be forced into an email process. The agent calls the public number, explains the source and preview, asks for consent to continue, and records the result as attempted, connected, follow up, qualified, not interested, no answer, or other.

**Technical callout:**

- `send_email` is rejected for phone-channel tickets.
- `phone_status` is rejected for email-channel tickets.
- Public customer messages and private agent notes are intentionally separated.

### Segment 11 — Cancellation, 10-day timing, and recovery

**Explain first:**

> The production rule does not cancel immediately after deployment. The deployed-notification macro first moves the ticket to pending and starts the clock. If the customer does not respond after 240 pending hours, Zendesk adds the cancellation-due tag and unchecks Deploy site.

**Show the sequence:**

1. `asf_customer_notified_deployed` and `asf_10_day_clock_started`.
2. Automation condition: 240 pending hours.
3. `asf_10_day_cancellation_due` and deploy field changed to false.
4. Cancellation webhook.
5. Netlify site disabled and ticket live URL cleared.
6. `asf_deployment_cancelled` / cancelled stage.
7. Email customer macro, or phone agent internal call note/script.

**For a live same-day demo:**

- Use a dedicated test ticket and a temporary test-only trigger/tag rather than editing the production 240-hour automation casually.
- Confirm site disable first.
- Confirm email macro/public message or phone private note second.
- Restore the 240-hour production condition immediately.

**Technical callout:**

- Cancellation can recover the exact Netlify site from the Zendesk live URL after an ephemeral backend restart.
- GitHub is deliberately retained for audit and redeployment.
- Rechecking Deploy can re-enable/redeploy the same artifact.
- Initial deployment writes the checkbox state before live tags, preventing an accidental cancellation trigger.

### Segment 12 — Deployment reporting, cost control, and wrap-up

**Screen:** `/deployments`, then `/overview`.

**Say:**

> The deployment ledger connects each channel request to its campaign, canonical lead, approval, repository, deployment history, status, and live URL. The business can answer how many leads were found, how many tickets were created, how many agents requested sites, how many AI generations occurred, how many repositories exist, and how many sites are live, pending, failed, or cancelled.

> For a 10,000-lead campaign, the system does not need to generate 10,000 sites. If agents select 5%, only 500 incur first-time AI and production-deploy usage. This deferred model is the main cost-control feature.

**Be transparent about the current gap:**

> The system records logical AI generations but does not yet store the exact input and output tokens returned by each provider. A provider-usage ledger is the next required financial-control feature before large volume.

**Close with:**

> AI Site Factory now demonstrates the full controlled lifecycle: lead discovery, mixed-channel campaign, Zendesk intake, agent approval, personalized artifact, GitHub and Netlify deployment, customer or call follow-up, reporting, cancellation, and recovery. The next stage is production hardening: current model migration, guaranteed persistence, background jobs, cost metering, and repeatable Zendesk automation packaging.

## 7. Technical deep-dive appendix

### 7.1 Key identifiers

| Identifier | Why it matters |
|---|---|
| Campaign ID | Groups lead generation, channel records, and graphs. |
| Canonical lead key | Represents the real business across batches/channels and prevents duplicate artifacts. |
| Pipeline ID | Correlates execution and step history. |
| Approval ID | Links ticket action to generated artifact/deployment approval. |
| Zendesk ticket ID/external ID | Links agent workflow and provides idempotent ticket creation. |
| GitHub repo + commit SHA | Durable generated artifact identity. |
| Netlify site/build/deploy IDs | Hosting identity and deployment audit. |

### 7.2 Main deployment webhook

```http
POST https://<backend>/api/zendesk/webhook
Content-Type: application/json
x-ai-site-factory-secret: <shared secret>
```

```json
{
  "action": "deploy_site",
  "approvalId": "<ticket approval field>",
  "canonicalLeadKey": "<ticket canonical key field>",
  "zendeskTicketId": "<ticket id>",
  "channel": "email",
  "actor": "Zendesk"
}
```

Supported action families:

- `deploy_site`
- `cancel_deployment`
- `send_email`
- `phone_status`

Typical error meanings:

- 401: webhook secret mismatch.
- 404: managed approval/ticket could not be resolved.
- 409: approval/canonical/ticket/channel conflict or invalid state.
- 502: downstream generation, export, deploy, or ticket-contract failure.

### 7.3 Deployment state sequence

```text
AWAITING_DEPLOYMENT
→ DEPLOY_REQUESTED / GENERATING
→ ARTIFACT_READY / REPO_READY
→ DEPLOYING
→ DEPLOYED / LIVE
→ CANCELLED (optional)
```

Failure branches preserve the most advanced safe artifact:

- generation failed → retry model stage;
- GitHub export failed after HTML → reuse HTML and retry GitHub;
- Netlify failed → reuse repo/artifact and retry deploy;
- Zendesk live update failed → keep deployment metadata and repair ticket contract.

### 7.4 Persistence and recovery

- SQLite is the operational registry.
- GitHub is the durable generated HTML artifact.
- Zendesk carries externally recoverable ticket identity/status.
- Netlify carries site identity/live state.
- Startup can restore seed data and selected managed Zendesk records.
- A paid Render persistent disk or managed database remains required for complete production durability.

## 8. Questions the presenter should be ready to answer

**Why require mixed leads?**  
The campaign is designed to demonstrate and operate both distinct Zendesk workflows. It will not create a misleading “mixed” campaign from only phone or only email data.

**Why are email leads hard to find?**  
Google Maps commonly exposes phone numbers. Emails often come from a business website, which conflicts with the deliberate no-website qualification rule. The honest solution is broader search, lawful enrichment, or verified upload—not invented addresses.

**Why not generate all sites immediately?**  
That spends model, repository, Netlify, and operational capacity before an agent has assessed the lead. Deferred generation aligns cost with agent intent.

**Why GitHub and Netlify?**  
GitHub provides a durable, inspectable artifact and commit history. Netlify provides managed build/hosting and site disable/re-enable APIs.

**Does unchecking deploy delete the repo?**  
No. It disables the Netlify site and clears live state while retaining GitHub for audit/redeployment.

**Does the app email phone-only leads?**  
No. Phone records use a call script/status workflow.

**Does changing the admin password break Zendesk webhooks?**  
No. Webhooks use their own shared secret. The password change only invalidates administrator browser sessions.

**Can it handle 10,000 leads now?**  
The import schema is chunked and resumable, but generating/deploying thousands requires production hardening: persistent storage, a durable job queue, provider throttling, cost ledger/budgets, and confirmed account quotas.

## 9. Password-change quick reference

Generate:

```powershell
.\.venv\Scripts\python.exe scripts\hash_admin_password.py
```

Then replace `ADMIN_PASSWORD_HASH` in Render’s **ai-site-factory-backend → Environment**, save/restart, and sign in with the new plain password. Do not change Vercel and do not paste the plain password into `ADMIN_PASSWORD_HASH`. Existing sessions become invalid automatically.

For full instructions and recovery steps, see [Monthly Progress Report — Password-change procedure](MONTHLY_PROGRESS_REPORT.md#10-password-change-procedure).

## 10. Demo fallback plan

| Problem | Safe fallback during presentation |
|---|---|
| Render cold/unavailable | Open `/api/health`, wait for READY, and explain backend hosting while showing existing screenshots/data. |
| Login fails | Do not expose credentials; switch to prepared screenshots and troubleshoot after the session. |
| Apify returns no email leads | Use the prepared verified mixed upload; explain the no-website/email data constraint. |
| Zendesk trigger does not fire | Show webhook activity and the expected body/secret contract; do not repeatedly toggle a real ticket. |
| Gemini/provider unavailable | Explain the saved-artifact/retry path and show a previously deployed personalized site. |
| GitHub failure | Show saved HTML/retry-export design and a previous repository. |
| Netlify credits/build unavailable | Show repository artifact and previous deployment; do not claim a live success. |
| 10-day process cannot be observed live | Show the production automation conditions, tags, macro, and an already completed test ticket. |

The fallback principle is to be explicit about what is live, what is previously verified, and what is being illustrated. Never turn an external provider failure into repeated billable clicks.

