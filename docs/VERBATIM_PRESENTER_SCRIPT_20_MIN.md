# AI Site Factory — Verbatim 20-Minute Presenter Script

**Target duration:** 20 minutes  
**Audience:** Business stakeholders, Zendesk administrators, technical reviewers, and project sponsors  
**Presentation goal:** Demonstrate how AI Site Factory finds or imports legitimate businesses without websites, creates controlled Zendesk work queues, and generates a personalized website only after an agent requests deployment.

Text in quotation blocks is spoken word for word. Text in bold square brackets is an action and must not be read aloud.

## 1. Set up the browser tabs before presenting

Use one browser window. Close unrelated tabs and arrange the following tabs from left to right in this exact order.

| Tab | Page to prepare | Why it is ready |
|---:|---|---|
| 1 | AI Site Factory login or Overview | Main presentation tab. Navigate within the application from here. |
| 2 | One successful Apify Google Maps actor run and its output | Prepared evidence if a live search is still running. Do not expose the API token. |
| 3 | One undeployed AI Site Factory Zendesk ticket | Safe ticket used to request a deployment. |
| 4 | Zendesk Admin Center — `AI Site Factory - Ticket actions` webhook | Shows the authenticated connection to the backend. |
| 5 | Zendesk Admin Center — Triggers filtered to `AI Site Factory` | Shows the five event-driven rules. |
| 6 | Zendesk Admin Center — Macros filtered to `AI Site Factory` | Shows the reusable agent actions and messages. |
| 7 | Zendesk Admin Center — Automations filtered to `AI Site Factory` | Shows the time-based 10-day rules. |
| 8 | One previously generated GitHub repository | Reliable artifact evidence during a live deployment wait. |
| 9 | The corresponding Netlify deploy page | Shows hosting status, site identity, and production URL. |
| 10 | The corresponding live generated website | Shows the finished personalized result. |
| 11 | One completed 10-day cancellation test ticket | Shows the cancellation result without changing the 240-hour production rule. |

### Keep the tab order simple

1. Do not open new tabs during the presentation unless a live URL must be demonstrated.
2. Move only from left to right until the live-site section.
3. Return to Tab 3 once after the deployment evidence.
4. Return to Tab 1 for the final reporting section.
5. Keep Tabs 8–10 as prepared evidence even when the live deployment succeeds.

### Prepare these records

- A small live lead search with a target of 2–5 leads.
- A prepared campaign with at least one email lead and one call lead.
- A safe undeployed Zendesk test ticket.
- A previously completed deployment whose GitHub repository, Netlify deploy, and live URL all match.
- A completed cancellation example with no real customer impact.
- A designated test recipient if the email action will be demonstrated.

### Never display

- API tokens;
- administrator passwords;
- webhook secrets;
- session cookies;
- Render environment-variable values;
- real customer information that is not approved for the demonstration.

## 2. Twenty-minute run sheet

| Time | Tab | Section | Presenter objective |
|---:|---:|---|---|
| 0:00–1:00 | 1 | Opening and login | State the business problem, controlled workflow, and security boundary. |
| 1:00–2:20 | 1 | Overview | Explain the full Apify → Zendesk → AI/GitHub → Netlify flow. |
| 2:20–4:20 | 1 | New campaign and Apify | Launch a small search, explain qualification, and leave it running in the background. |
| 4:20–5:40 | 1, then 2 if needed | Lead workspace and Apify evidence | Show email/call queues and use prepared Apify output if the live search is not ready. |
| 5:40–7:10 | 3 | Zendesk ticket | Explain forms, fields, requester identity, and the approval checkbox. |
| 7:10–8:05 | 4 | Webhook | Explain the authenticated HTTP connection and idempotency. |
| 8:05–9:15 | 5 | Triggers | Explain immediate event-driven rules and the five managed triggers. |
| 9:15–10:10 | 6 | Macros | Explain reusable manual agent actions and channel-specific messages. |
| 10:10–11:05 | 7 | Automations | Explain time-based rules and the 240-pending-hour cancellation clock. |
| 11:05–12:20 | 3 | Request deployment | Submit once and explain the cost-control boundary. |
| 12:20–14:45 | 8–9 | Processing wait | Explain generation, persistence, GitHub, and Netlify while the live job continues. |
| 14:45–16:15 | 10 | Generated website | Demonstrate personalization, grounding, responsive behavior, and contact actions. |
| 16:15–17:20 | 3 | Email and call workflows | Explain approved email and agent-led phone follow-up. |
| 17:20–18:30 | 11 | Cancellation | Show the completed 10-day outcome and recovery design. |
| 18:30–19:30 | 1 | Deployments and reporting | Show lifecycle visibility and cost control. |
| 19:30–20:00 | 1 | Closing | Summarize the business value and invite questions. |

## 3. Complete verbatim presenter script

### 0:00–1:00 — Opening and secure login

**[Show Tab 1. Keep the AI Site Factory login page or Overview visible.]**

> Welcome. This is AI Site Factory, a lead-to-site campaign platform. It finds or imports public businesses that do not have websites, creates structured Zendesk work queues, and lets an agent decide which businesses should receive a personalized website.

> The important principle is controlled spending. Finding a lead does not immediately call the AI models, create a GitHub repository, or deploy a Netlify site. Those stages begin only after an agent requests deployment from Zendesk.

**[If the login page is visible, enter the prepared credentials privately and select Sign in securely.]**

> The application uses a dedicated administrator login. The backend validates the password and returns a signed HTTP-only session cookie. Provider credentials and webhook secrets remain on the backend and are not stored in the frontend.

### 1:00–2:20 — Overview and system flow

**[Show Overview. Point across the system-flow banner and metric cards.]**

> This is the campaign command centre. The complete business flow is shown here. Apify supplies public business-listing data. Zendesk owns the agent and customer workflow. AI creates the personalized website after approval. GitHub stores the generated artifact, and Netlify publishes it.

> The frontend is a React and Vite application hosted on Vercel. It communicates over HTTPS with a FastAPI backend hosted on Render. The backend owns authentication, business rules, provider calls, workflow state, duplicate protection, and recovery.

**[Point to campaign, lead, deployment, AI generation, repository, pending, and failure metrics.]**

> These metrics let an administrator see how many campaigns and lead records exist, how many websites were generated, how many repositories were created, and how many deployments are live, pending, or failed.

> The dashboard is not only a presentation layer. It is a lifecycle view connecting each campaign, lead, Zendesk ticket, approval, generated artifact, repository, deployment, and live URL.

### 2:20–4:20 — New campaign and Apify

**[Select New campaign and stay on Find leads.]**

> A campaign can begin in two ways. We can run a public lead search through Apify, or we can upload an existing CSV, JSON, or JSONL file.

**[Point to campaign name, location, preset, industry, search intent, lead target, and force-refresh option.]**

> The administrator can choose a preset or enter any custom industry and search intent. The lead target can be between 2 and 100. The application can also generate the campaign name and stored industry from the verified results.

> Apify is the public-data discovery service in this workflow. The backend starts a focused Google Maps actor run, checks its progress, and reads its dataset. The search belongs to the backend, so leaving this page does not cancel it.

> The application does not invent businesses, phone numbers, or email addresses. A returned lead must have a real public phone number or a valid email address, and the business must not already have a website. A business can qualify with either contact channel; it does not need to have both.

**[Set the target to 2–5 leads. Confirm the location and industry. Select Launch campaign once.]**

> I am starting a small live search. The status is saved as a background job. I can now leave this page, and Apify will continue working while I demonstrate another part of the application.

**[Immediately navigate to Lead workspace. Do not remain on the form waiting.]**

> This background behavior is also used for lead-file uploads. Navigation and page refreshes do not own or stop the backend job.

### 4:20–5:40 — Lead workspace and prepared Apify evidence

**[Select the prepared campaign in Lead workspace.]**

> This campaign has separate email and call work queues. An email lead has a usable email address. A call lead has a public phone number. If one business contains both, it can support both communication routes without duplicating the website identity.

**[Show Email Leads, then Call Leads. Point to business, contact, source, Zendesk ticket, deploy request, and status.]**

> Each row keeps the business information, contact method, public source, Zendesk ticket, deployment request, and lifecycle status together.

> The backend creates a canonical identity for each business. This prevents a second website from being generated when the same business appears in another search, upload, or channel.

**[If the live search has completed, briefly show its result. If it is still running, switch to Tab 2.]**

> The live search is still processing, so I will use this prepared successful Apify run as evidence rather than create dead time.

**[On Tab 2, point to the actor status and dataset rows. Do not reveal the token.]**

> This is the Apify actor output. These are public listing records returned by the provider. AI Site Factory then applies its own rules: remove businesses with websites, reject missing or malformed contact details, normalize the fields, and deduplicate the identities.

**[Return to Tab 3.]**

### 5:40–7:10 — Zendesk ticket and approval boundary

**[Show the prepared undeployed Zendesk ticket.]**

> This is the agent workspace. The requester is named after the business rather than being incorrectly assigned to the API administrator. The business does not need to have an existing Zendesk account.

**[Point to the form name, campaign fields, business fields, contact channel, source, status, Deploy site checkbox, and Live site URL.]**

> The ticket uses the correct Email Lead or Call Lead form. It contains the campaign identity, canonical business identity, public contact information, location, source, workflow status, and deployment controls.

> The first note is internal. It tells the agent that this is a verified lead and that no website has been generated yet.

> The Deploy site checkbox is the approval and cost-control boundary. At this moment the ticket exists, but the lead does not yet require a new AI generation, repository, or Netlify deployment.

> AI Site Factory provisions Zendesk resources in dependency order: fields first, forms second, views third, and the webhook and triggers last. New triggers start inactive so an administrator can review and test them before activation.

### 7:10–8:05 — Webhook

**[Switch to Tab 4 and show `AI Site Factory - Ticket actions`.]**

> A webhook is an authenticated HTTP connection from Zendesk to the FastAPI backend. It is the bridge that turns a ticket action into backend work.

> When a managed trigger fires, Zendesk sends a POST request containing the action, approval ID, canonical business key, ticket ID, and channel. The request also contains a secret header that is separate from the administrator password.

> The backend validates the secret and confirms that the ticket, approval, business identity, and channel agree. Duplicate deliveries are idempotent, meaning a repeated webhook does not create a second site.

> This single webhook supports four action types: deploy a site, cancel a deployment, send an approved email, and record a phone status.

**[Point only to the active status and public HTTPS endpoint. Do not open authentication values.]**

### 8:05–9:15 — Triggers

**[Switch to Tab 5 and show the AI Site Factory triggers.]**

> A trigger is an immediate event-driven Zendesk rule. It checks a ticket when the ticket is created or updated. If its conditions match, it performs its actions straight away.

> AI Site Factory uses five core triggers. Deploy email lead watches the deployment checkbox on the email form. Deploy call lead watches the same checkbox on the phone form. Cancel email deployment and Cancel call deployment detect an approved site being unchecked. Send approved email watches the separate email-approval checkbox after a live site exists.

**[Point to the five names without editing them.]**

> Each trigger sends a channel-specific action to the webhook and adds a one-shot guard tag. The guard tag prevents the same ticket update from firing the same action repeatedly.

> Triggers are different from automations. A trigger responds to an event now. An automation checks time-based conditions on a schedule.

### 9:15–10:10 — Macros

**[Switch to Tab 6 and show the AI Site Factory macros.]**

> A macro is a reusable bundle of ticket actions that an agent can apply manually. It can insert a prepared public reply or private note, add tags, change fields, and set a ticket status consistently.

> Macros help agents use approved wording instead of rewriting the same message for every lead. The deployed-site notification starts the customer follow-up and 10-day clock. The email cancellation macro is named AI Site Factory, Email, 10-day cancellation, notify customer. The phone cancellation macro provides the agent’s call script rather than sending an email to a phone-only lead.

> A macro is not a timer and it does not watch ticket events. The agent applies it, or a controlled backend process renders and applies the approved actions when the workflow requires it.

**[Do not apply a public-message macro unless the designated test recipient is selected.]**

### 10:10–11:05 — Automations

**[Switch to Tab 7 and show the 10-day automation conditions.]**

> An automation is a time-based Zendesk rule. Zendesk evaluates automations on a schedule and acts when a ticket has remained in a matching state for the required period.

> After the deployed-site notification, the ticket moves to pending and receives the clock-started tags. The production cancellation period is 240 pending hours, which is 10 days.

> When the period expires without the required response, the email or phone automation adds the cancellation-due tag and unchecks Deploy site. That ticket change allows the correct cancellation trigger to call the backend.

> I am not shortening or editing the production 240-hour condition during this presentation. Later I will show a completed test ticket as safe evidence.

### 11:05–12:20 — Request a deployment

**[Return to Tab 3. Confirm that this is the safe undeployed ticket.]**

> I will now request one deployment. Before I submit, the lead has a controlled Zendesk record but no newly generated site.

**[Check `AI Site Factory - Deploy site` and submit the ticket once.]**

> The trigger has sent the authenticated deploy action to the backend. I will not toggle the checkbox repeatedly. The backend protects the request with an idempotent deployment claim.

> The backend first builds a grounded business brief from the public lead data. Groq supports the briefing stage, and Gemini produces the final single-file HTML website.

> The generated HTML is validated and saved before GitHub is called. That separation matters because a temporary GitHub or Netlify failure can be retried without paying for another AI generation.

**[If a private “deployment requested” note appears, point to it. Then move to Tab 8.]**

### 12:20–14:45 — What to say while deployment is processing

**[Show Tab 8, the prepared matching GitHub repository.]**

> The live deployment is continuing in the background. While it runs, this prepared example shows the same artifact stage.

> GitHub is the durable website-artifact layer. The repository contains the generated index file, a README, a stable repository identity, and a commit history. This makes the generated site inspectable and recoverable.

**[Point to `index.html`, `README.md`, the repository name, and the latest commit. Do not open repository secrets or settings.]**

> The backend creates or recovers one lead-owned repository. If the artifact already exists, the workflow can reuse it instead of generating another business site.

**[Switch to Tab 9, the matching Netlify deployment.]**

> Netlify is the hosting and deployment layer. It connects the GitHub artifact to a production build and provides the public URL.

> The backend records the Netlify site ID, build ID, deploy ID, state, and live URL. Those identifiers allow the application to audit the deployment, disable the site after cancellation, re-enable it, or redeploy the retained GitHub artifact.

**[Point to the production status, linked repository, and URL. Do not open Netlify tokens or environment variables.]**

> If this live job completes during the presentation, the same Zendesk ticket will receive the live URL, deployed status, lifecycle tags, and a private success note. If it needs longer, the prepared repository, deployment, and live site let us continue without pretending that the provider has already finished.

### 14:45–16:15 — Generated website

**[Switch to Tab 10 and show the live generated site.]**

> This is the finished customer-facing result. The page is grounded in the information available for this specific business. It is not only a generic template with the company name replaced.

**[If this lead returned a source image, point to the hero image. Then point to the business name, caption, palette, services, location, and contact actions.]**

> When the lead source returns a valid business image, the generated site uses that exact image prominently. If the source returns no image, the page remains intentionally image-free instead of inventing or substituting an unrelated picture. The page also includes an industry-appropriate color palette, a personalized caption, four distinct service sections, location context, and working call, email, and navigation actions.

> The generator is not allowed to invent awards, qualifications, prices, employees, guarantees, phone numbers, email addresses, or unsupported services. If the AI output is incomplete or insufficiently personalized, the backend can reject, repair, or replace it with a grounded deterministic renderer.

**[Demonstrate one safe navigation action and briefly narrow the browser if responsive behavior is relevant.]**

> The result is designed for both desktop and mobile use. Prospect preview pages remain controlled workflow artifacts, and the system keeps the provider and ticket identities needed for audit and recovery.

### 16:15–17:20 — Email and call workflows

**[Return to Tab 3. Show either the completed live result or the prepared deployed ticket state.]**

> Deployment does not automatically send an unreviewed customer message. Email and phone leads continue through different controlled workflows.

> For an email lead, the agent reviews the website and prepared message. The separate Send approved email action can then post the approved public reply containing the live link.

> For a call lead, the live URL remains on the ticket for the assigned agent. The agent uses the approved call script, speaks to the business, and records the outcome using the call-status field.

> A phone-only lead is never forced into an email process, and an email-only lead does not require a fabricated phone number.

**[If the live deployment completed, point to the private success note, deployed status, and populated Live site URL. Otherwise, say the next paragraph.]**

> The live provider job is still completing, so I will leave this ticket unchanged and use the prepared successful evidence already shown. I will not submit a duplicate deployment request.

### 17:20–18:30 — Ten-day cancellation and recovery

**[Switch to Tab 11 and show the completed cancellation test ticket.]**

> This ticket demonstrates the end of the 10-day workflow without changing the production timer.

> The deployed-site notification started the clock by moving the ticket to pending. After 240 pending hours, the automation added the cancellation-due tag and unchecked Deploy site. The cancellation trigger then sent the cancel-deployment action through the authenticated webhook.

> The backend disabled the Netlify site, cleared the live URL, updated the lifecycle state, and retained the GitHub repository.

> For the email channel, the approved cancellation macro creates the public customer message only after the site is disabled. For the phone channel, the ticket is reopened with a private instruction for the agent to make the cancellation call using the phone script.

> Retaining GitHub is intentional. If the business wants to continue later, rechecking Deploy site can recover and redeploy the existing artifact rather than generating everything again.

### 18:30–19:30 — Deployments, reporting, and cost control

**[Return to Tab 1. Open Deployments, then Overview.]**

> The deployment ledger connects the campaign, business, communication channel, Zendesk ticket, approval identity, AI generation, GitHub repository, Netlify deployment, and live URL.

> This allows the business to report how many leads were discovered or uploaded, how many Zendesk records were created, how many agents requested websites, how many AI generations occurred, how many repositories were created, and how many sites are pending, live, failed, or cancelled.

> The main cost control is deferred generation. A campaign may contain thousands of leads, but only agent-approved leads require first-time AI generation and deployment resources.

### 19:30–20:00 — Closing

**[Stop navigating and face the audience.]**

> In summary, AI Site Factory connects verified lead discovery, Zendesk-controlled approval, grounded AI generation, auditable GitHub storage, Netlify hosting, channel-specific customer follow-up, cancellation, recovery, and campaign reporting in one controlled workflow.

> Thank you. I am happy to demonstrate any individual stage again or answer questions about Apify, Zendesk macros, triggers, automations, webhook security, AI generation, GitHub, Netlify, or the business process.

## 4. Waiting and recovery branches

Use only the matching branch. Do not read every branch aloud.

### If Apify finishes quickly

> The background search has completed. We can see the verified records and their Zendesk intake status. The search continued even though I left the campaign page.

### If Apify is still running

> Apify is still processing the public listing search. The job is owned by the backend, so I can continue the demonstration without keeping the campaign page open. I will use the prepared verified campaign and return to the live result later if needed.

### If Apify fails

> The provider returned an error, and the application did not invent replacement contacts. The failed job remains auditable. I will use the prepared successful dataset and continue with the controlled Zendesk workflow.

Do not repeatedly start new searches during the presentation.

### If AI generation or deployment finishes quickly

> The deployment has completed. The same ticket now contains the live URL, lifecycle status, deployment tags, and private success note.

### If AI generation, GitHub, or Netlify is still running

> The provider workflow is still completing. The request is saved, so I will not submit it again. I will use the prepared GitHub, Netlify, and live-site evidence while it continues.

### If GitHub fails after the artifact is saved

> The generated HTML was saved before the repository step. The GitHub export can be retried without paying for another AI generation.

### If Netlify is still building

> Netlify is still building the saved GitHub artifact. The repository and commit already provide durable evidence, and the production URL will be written back to Zendesk when the build is ready.

### If the deployment fails

> The failure is recorded against the workflow stage and ticket. I will not repeatedly toggle the approval field. The operator can retry the failed provider stage while reusing any artifact or repository that already succeeded.

### If the 10-day process cannot be demonstrated live

> This workflow intentionally lasts 240 pending hours, so it is not appropriate to wait for it or edit the production rule during a presentation. This prepared completed ticket shows the tested outcome.

## 5. Plain-language terminology cheat sheet

| Term | Simple description | When it acts |
|---|---|---|
| Apify | Runs the public Google Maps business search and stores the returned dataset. | When an administrator launches a lead search. |
| Macro | A reusable bundle of Zendesk ticket actions or approved wording applied by an agent or controlled backend process. | Manually or at a specifically controlled workflow step. |
| Trigger | An immediate Zendesk rule that evaluates a ticket after a creation or update event. | As soon as matching ticket conditions change. |
| Automation | A scheduled Zendesk rule based on elapsed time and ticket state. | During Zendesk’s periodic automation evaluation. |
| Webhook | An authenticated HTTP request carrying the ticket action and identity to the backend. | When a trigger calls the backend. |
| GitHub | Stores the validated generated website artifact and commit history. | After generation passes validation. |
| Netlify | Builds, hosts, disables, re-enables, and redeploys the website artifact. | After GitHub is ready or during recovery/cancellation. |

### The five managed triggers

| Trigger | Description |
|---|---|
| `AI Site Factory - Deploy email lead` | Starts site deployment from an approved email-lead ticket. |
| `AI Site Factory - Deploy call lead` | Starts site deployment from an approved call-lead ticket. |
| `AI Site Factory - Cancel email deployment` | Cancels an existing email-lead deployment when the deploy field is unchecked. |
| `AI Site Factory - Cancel call deployment` | Cancels an existing call-lead deployment when the deploy field is unchecked. |
| `AI Site Factory - Send approved email` | Sends the reviewed live-link message for a deployed email lead. |

### Macro examples

| Macro | Description |
|---|---|
| Deployed-site/customer notification | Shares the deployed result through the approved channel, adds clock-start tags, and moves the ticket to pending. |
| `AI Site Factory::Email::10-day cancellation - notify customer` | Creates the approved public email cancellation message after Netlify is disabled. |
| `AI Site Factory::Phone::10-day cancellation - call script` | Gives the agent the approved phone cancellation script through an internal workflow. |

### Ten-day automation sequence

1. A deployed-notification action adds the clock-start tags and sets the ticket to pending.
2. Zendesk waits for 240 pending hours.
3. The matching email or phone automation adds the cancellation-due tag and unchecks Deploy site.
4. The matching cancellation trigger calls the webhook.
5. The backend disables Netlify and clears the live URL.
6. The email macro or phone-agent instruction runs only after cancellation succeeds.
7. GitHub remains available for recovery.

## 6. Final two-minute preparation checklist

- [ ] Tabs are in the exact 1–11 order.
- [ ] The live Apify target is only 2–5.
- [ ] The prepared campaign contains email and call examples.
- [ ] The deployment ticket is safe, undeployed, and not already processing.
- [ ] The webhook tab does not expose its secret.
- [ ] Trigger, macro, and automation tabs are filtered to AI Site Factory.
- [ ] GitHub, Netlify, and live-site evidence all refer to the same business.
- [ ] The cancellation ticket is a completed test record.
- [ ] No real email or phone action will be sent unintentionally.
- [ ] The production 240-hour automation will not be edited.

## 7. One-sentence emergency close

> AI Site Factory finds or imports legitimate businesses without websites, lets Zendesk agents control when generation and deployment spending begins, stores each grounded website in GitHub, publishes it through Netlify, and returns the complete customer lifecycle to Zendesk and the campaign dashboard.
