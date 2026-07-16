import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import axios from "axios";
import {
  AlertTriangle,
  BarChart3,
  BriefcaseBusiness,
  Check,
  ChevronRight,
  CircleUserRound,
  CloudCog,
  Database,
  ExternalLink,
  Eye,
  GitBranch,
  LayoutDashboard,
  Lock,
  Mail,
  MapPin,
  Phone,
  Plus,
  RefreshCw,
  Rocket,
  Settings2,
  ShieldCheck,
  TicketCheck,
  Unplug,
  Upload,
  UsersRound,
  Webhook,
  WandSparkles,
  Zap,
} from "lucide-react";
import "./App.css";

const DEFAULT_API_BASE = import.meta.env.PROD
  ? "https://ai-site-factory-backend-c4w6.onrender.com"
  : "http://127.0.0.1:8000";
const API_BASE = (import.meta.env.VITE_API_BASE || DEFAULT_API_BASE).replace(/\/$/, "");

const FALLBACK_PRESETS = [
  { id: "restaurants", label: "Restaurants", industry: "Restaurant", description: "Restaurants, cafes, and takeaways." },
  { id: "plumbers", label: "Plumbers", industry: "Plumbing", description: "Local plumbing and maintenance teams." },
  { id: "dentists", label: "Dentists", industry: "Dental", description: "Dental practices and oral care providers." },
  { id: "beauty-salons", label: "Beauty Salons", industry: "Beauty", description: "Salons, spas, and beauty studios." },
  { id: "electricians", label: "Electricians", industry: "Electrical", description: "Electrical installation and repair services." },
  { id: "cleaning-services", label: "Cleaning Services", industry: "Cleaning", description: "Home, office, and specialist cleaning." },
];

const NAV_ITEMS = [
  { to: "/overview", label: "Overview", icon: LayoutDashboard },
  { to: "/campaigns", label: "New campaign", icon: Plus, requiresSetup: true },
  { to: "/leads", label: "Lead workspace", icon: UsersRound, requiresSetup: true },
  { to: "/deployments", label: "Deployments", icon: Rocket, requiresSetup: true },
  { to: "/zendesk", label: "Zendesk setup", icon: Settings2 },
];

const FIELD_LABELS = {
  campaignId: "Campaign ID",
  campaignName: "Campaign name",
  canonicalLeadKey: "Canonical lead key",
  pipelineId: "Pipeline ID",
  approvalId: "Approval ID",
  batchId: "Apify batch ID",
  businessName: "Business name",
  contactName: "Contact name",
  contactEmail: "Contact email",
  contactPhone: "Contact phone",
  industry: "Industry",
  location: "Location",
  address: "Address",
  contactChannel: "Contact channel",
  leadStatus: "Lead status",
  deployRequested: "Deploy requested checkbox",
  emailSendRequested: "Email send checkbox",
  phoneCallStatus: "Phone call status",
  liveUrl: "Live website URL",
  sourceUrl: "Lead source URL",
};

function errorMessage(error) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  return detail?.message || error?.message || "Something went wrong.";
}

function formatDate(value) {
  if (!value) return "Not yet";
  return new Date(value).toLocaleString();
}

function statusTone(status = "") {
  const value = String(status).toUpperCase();
  if (["DEPLOYED", "APPROVED", "COMPLETED", "REUSED_DEPLOYMENT", "READY", "CONNECTED", "TICKET_READY"].includes(value)) return "success";
  if (["FAILED", "GENERATION_FAILED", "DEPLOY_FAILED", "EXPORT_FAILED", "NEEDS_ATTENTION"].includes(value)) return "danger";
  if (["DEPLOYING", "GENERATING", "DEPLOY_REQUESTED", "ARTIFACT_READY"].includes(value)) return "active";
  return "pending";
}

function StatusBadge({ status }) {
  return <span className={`status-badge ${statusTone(status)}`}>{String(status || "Pending").replaceAll("_", " ")}</span>;
}

function PageSection({ title, eyebrow, description, action, children, className = "" }) {
  return (
    <section className={`surface ${className}`}>
      <div className="section-heading">
        <div>
          {eyebrow && <span className="eyebrow">{eyebrow}</span>}
          <h2>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function EmptyState({ icon: Icon = Database, title, text }) {
  return (
    <div className="empty-state">
      <Icon size={30} />
      <h3>{title}</h3>
      <p>{text}</p>
    </div>
  );
}

function WorkspaceLocked({ connection }) {
  return (
    <div className="workspace-locked">
      <div><Lock size={28} /></div>
      <span className="eyebrow">Zendesk required</span>
      <h2>Finish the Zendesk workspace setup first.</h2>
      <p>
        Campaigns cannot be stored in an offline queue. Connect the instance, select an existing brand,
        and provision the managed fields and Email/Call forms before this workspace becomes available.
      </p>
      <NavLink className="primary-button" to="/zendesk"><Settings2 size={17} />{connection.connected ? "Finish Zendesk setup" : "Connect Zendesk"}</NavLink>
    </div>
  );
}

function SetupGuard({ connection, children }) {
  return connection.workspaceReady ? children : <WorkspaceLocked connection={connection} />;
}

function MetricCard({ icon: Icon, label, value, note, tone = "blue" }) {
  return (
    <article className={`metric-card ${tone}`}>
      <div className="metric-icon"><Icon size={20} /></div>
      <div><span>{label}</span><strong>{Number(value || 0).toLocaleString()}</strong><small>{note}</small></div>
    </article>
  );
}

function DonutChart({ deployed = 0, pending = 0, failed = 0 }) {
  const actualTotal = deployed + pending + failed;
  const total = Math.max(1, actualTotal);
  const liveStop = (deployed / total) * 100;
  const pendingStop = liveStop + (pending / total) * 100;
  return (
    <div className="donut-wrap">
      <div
        className="donut"
        style={{ background: actualTotal ? `conic-gradient(#10b981 0 ${liveStop}%, #f59e0b ${liveStop}% ${pendingStop}%, #ef4444 ${pendingStop}% 100%)` : "#e8edf5" }}
        role="img"
        aria-label={`${deployed} live, ${pending} pending, ${failed} failed`}
      >
        <div><strong>{deployed + pending + failed}</strong><span>deployment records</span></div>
      </div>
      <div className="chart-legend">
        <span><i className="live" />Live <strong>{deployed}</strong></span>
        <span><i className="pending" />Pending <strong>{pending}</strong></span>
        <span><i className="failed" />Failed <strong>{failed}</strong></span>
      </div>
    </div>
  );
}

function FunnelChart({ items = [] }) {
  const max = Math.max(1, ...items.map((item) => Number(item.value) || 0));
  return (
    <div className="funnel-chart">
      {items.map((item, index) => (
        <div className="funnel-row" key={item.label}>
          <span>{index + 1}</span>
          <label>{item.label}</label>
          <div className="bar-track"><i style={{ width: `${Math.max(item.value ? 7 : 0, (Number(item.value || 0) / max) * 100)}%` }} /></div>
          <strong>{Number(item.value || 0).toLocaleString()}</strong>
        </div>
      ))}
    </div>
  );
}

function CampaignComparison({ campaigns }) {
  if (!campaigns.length) return <EmptyState icon={BarChart3} title="No campaign data" text="Launch a campaign to populate this chart." />;
  const visible = campaigns.slice(0, 8);
  const max = Math.max(1, ...visible.map((item) => item.metrics.channelLeads || 0));
  return (
    <div className="comparison-chart">
      {visible.map((campaign) => {
        const metrics = campaign.metrics;
        const liveLeads = metrics.liveChannelLeads ?? metrics.deployed;
        return (
          <div className="comparison-row" key={campaign.campaignId}>
            <div><strong>{campaign.campaignName}</strong><span>{campaign.industry} · {campaign.location}</span></div>
            <div className="stacked-track" aria-label={`${liveLeads} live and ${metrics.pending} pending`}>
              <i className="live" style={{ width: `${(liveLeads / max) * 100}%` }} />
              <i className="pending" style={{ width: `${(metrics.pending / max) * 100}%` }} />
              <i className="failed" style={{ width: `${(metrics.failed / max) * 100}%` }} />
            </div>
            <strong>{liveLeads}/{metrics.channelLeads}</strong>
          </div>
        );
      })}
    </div>
  );
}

function CampaignPicker({ campaigns, selectedId, onChange }) {
  return (
    <label className="campaign-picker">
      Campaign
      <select value={selectedId || ""} onChange={(event) => onChange(event.target.value)}>
        <option value="">Select a campaign</option>
        {campaigns.map((campaign) => <option key={campaign.campaignId} value={campaign.campaignId}>{campaign.campaignName}</option>)}
      </select>
    </label>
  );
}

function OverviewPage({ campaigns, totals, onSelectCampaign }) {
  const populatedCampaigns = campaigns.filter((campaign) => campaign.metrics.channelLeads > 0);
  const visibleCampaigns = populatedCampaigns.length ? populatedCampaigns : campaigns;
  const aggregate = useMemo(() => campaigns.reduce((acc, campaign) => {
    campaign.funnel.forEach((item) => { acc[item.label] = (acc[item.label] || 0) + Number(item.value || 0); });
    return acc;
  }, {}), [campaigns]);
  const funnel = ["Discovered", "Channel records", "Zendesk", "Deploy requested", "AI generated", "Repos created", "Live"]
    .map((label) => ({ label, value: aggregate[label] || 0 }));
  const failed = campaigns.reduce((sum, item) => sum + (item.metrics.failed || 0), 0);
  return (
    <div className="page-stack">
      <section className="hero-banner">
        <div>
          <span className="eyebrow">Campaign command centre</span>
          <h1>Turn qualified leads into deployed sites, only when an agent says go.</h1>
          <p>Apify finds the lead, Zendesk owns the conversation, and AI generation starts only after the deploy checkbox webhook is fired.</p>
        </div>
        <div className="hero-flow">
          {["Apify", "Zendesk", "AI + GitHub", "Netlify"].map((label, index) => <span key={label}><i>{index + 1}</i>{label}{index < 3 && <ChevronRight size={16} />}</span>)}
        </div>
      </section>

      <div className="metric-grid">
        <MetricCard icon={BriefcaseBusiness} label="Campaigns" value={totals.campaigns} note="Named lead searches" tone="violet" />
        <MetricCard icon={UsersRound} label="Lead records" value={totals.leads} note="Email + call queues" tone="blue" />
        <MetricCard icon={Rocket} label="Live deployments" value={totals.deployments} note="Netlify sites ready" tone="green" />
        <MetricCard icon={Zap} label="AI generations" value={totals.aiGenerations} note="Existing + deploy-triggered" tone="orange" />
        <MetricCard icon={GitBranch} label="Repos created" value={totals.reposCreated} note="One artifact per lead" tone="cyan" />
        <MetricCard icon={CloudCog} label="Pending" value={totals.pending} note="Waiting for agent action" tone="amber" />
      </div>

      <div className="dashboard-grid">
        <PageSection title="Deployment health" eyebrow="All campaigns" description="Live, pending, and failed deployment records.">
          <DonutChart deployed={totals.deployments} pending={totals.pending} failed={failed} />
        </PageSection>
        <PageSection title="Campaign funnel" eyebrow="Conversion" description="Counts move from discovery to live deployment.">
          <FunnelChart items={funnel} />
        </PageSection>
      </div>

      <PageSection title="Campaign performance" eyebrow="Comparison" description="The green share is live; amber is still waiting on deployment approval.">
        <CampaignComparison campaigns={visibleCampaigns} />
      </PageSection>

      <PageSection title="Recent campaigns" eyebrow="Workspaces" description="Open a campaign to see its email and call queues.">
        {campaigns.length ? <div className="campaign-card-grid">{visibleCampaigns.slice(0, 6).map((campaign) => (
          <button className="campaign-card" type="button" key={campaign.campaignId} onClick={() => onSelectCampaign(campaign.campaignId)}>
            <div><span>{campaign.industry}</span><StatusBadge status={campaign.status} /></div>
            <h3>{campaign.campaignName}</h3>
            <p><MapPin size={14} />{campaign.location}</p>
            <div className="mini-metrics"><span><strong>{campaign.metrics.emailLeads}</strong>Email</span><span><strong>{campaign.metrics.callLeads}</strong>Call</span><span><strong>{campaign.metrics.deployed}</strong>Live</span></div>
          </button>
        ))}</div> : <EmptyState icon={BriefcaseBusiness} title="No campaigns yet" text="Create the first named campaign to start a lead intake." />}
      </PageSection>
    </div>
  );
}

function CampaignForm({ presets, connection, onCreated }) {
  const [form, setForm] = useState({
    campaignName: "",
    presetId: presets[0]?.id || "restaurants",
    industry: presets[0]?.industry || "Restaurant",
    location: "Durban, South Africa",
    query: "",
    limit: 10,
    email: true,
    phone: true,
    forceRefresh: false,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selectedPreset = presets.find((item) => item.id === form.presetId) || presets[0];
  const update = (patch) => setForm((current) => ({ ...current, ...patch }));

  useEffect(() => {
    if (!selectedPreset) return;
    setForm((current) => ({ ...current, industry: selectedPreset.industry }));
  }, [form.presetId, selectedPreset]);

  const submit = async (event) => {
    event.preventDefault();
    setError("");
    if (!form.email && !form.phone) {
      setError("Select at least one lead channel.");
      return;
    }
    setBusy(true);
    try {
      const { data } = await axios.post(`${API_BASE}/api/campaigns/intake`, {
        campaignName: form.campaignName,
        presetId: form.presetId,
        industry: form.industry,
        location: form.location,
        query: form.query || null,
        limit: Number(form.limit),
        channels: [form.email && "email", form.phone && "phone"].filter(Boolean),
        forceRefresh: form.forceRefresh,
        syncZendesk: true,
      }, { timeout: 900000 });
      onCreated(data);
      setForm((current) => ({ ...current, campaignName: "", forceRefresh: false }));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="campaign-form" onSubmit={submit}>
      <div className="form-grid two">
        <label>Campaign name<input required value={form.campaignName} onChange={(event) => update({ campaignName: event.target.value })} placeholder="e.g. Durban Plumbers - July" /></label>
        <label>Location<input required value={form.location} onChange={(event) => update({ location: event.target.value })} placeholder="City, province, or country" /></label>
      </div>
      <div className="preset-selector">
        {presets.map((preset) => (
          <button className={form.presetId === preset.id ? "selected" : ""} type="button" key={preset.id} onClick={() => update({ presetId: preset.id })}>
            <strong>{preset.label}</strong><span>{preset.description}</span>
          </button>
        ))}
      </div>
      <div className="form-grid three">
        <label>Industry<input value={form.industry} onChange={(event) => update({ industry: event.target.value })} /></label>
        <label>Search intent<input value={form.query} onChange={(event) => update({ query: event.target.value })} placeholder="e.g. emergency services" /></label>
        <label>Lead target<input type="number" min="1" value={form.limit} onChange={(event) => update({ limit: Math.max(1, Number(event.target.value) || 1) })} /></label>
      </div>
      <div className="channel-choice">
        <label className={form.email ? "checked" : ""}><input type="checkbox" checked={form.email} onChange={(event) => update({ email: event.target.checked })} /><Mail size={20} /><span><strong>Email leads</strong><small>Email form + email send checkbox</small></span></label>
        <label className={form.phone ? "checked" : ""}><input type="checkbox" checked={form.phone} onChange={(event) => update({ phone: event.target.checked })} /><Phone size={20} /><span><strong>Call leads</strong><small>Phone form + call status field</small></span></label>
      </div>
      <div className="form-options">
        <label><input type="checkbox" checked readOnly />Create Zendesk intake tickets (required)</label>
        <label><input type="checkbox" checked={form.forceRefresh} onChange={(event) => update({ forceRefresh: event.target.checked })} />Force a fresh Apify run</label>
      </div>
      <div className="deferred-note"><Zap size={22} /><div><strong>Cost-safe by design</strong><p>This step finds and tags leads only. Gemini, GitHub, and Netlify are not called until an agent requests deployment.</p></div></div>
      {error && <div className="inline-alert danger">{error}</div>}
      <div className="form-submit"><button className="primary-button" type="submit" disabled={busy || !connection.workspaceReady}>{busy ? <><RefreshCw className="spin" size={18} />Finding leads…</> : <><Rocket size={18} />Launch campaign</>}</button><span>Lead target is dynamic; Zendesk tickets are created before the workspace opens.</span></div>
    </form>
  );
}

function UploadCampaignForm({ connection, onCreated }) {
  const [form, setForm] = useState({ campaignName: "", industry: "Local service", location: "South Africa", email: true, phone: true, chunkSize: 5 });
  const [file, setFile] = useState(null);
  const [job, setJob] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const update = (patch) => setForm((current) => ({ ...current, ...patch }));

  useEffect(() => {
    if (!connection.workspaceReady) return;
    axios.get(`${API_BASE}/api/campaigns/imports?limit=10`, { timeout: 20000 })
      .then(({ data }) => {
        const active = (data.jobs || []).find((item) => !["COMPLETED"].includes(item.status));
        if (active) setJob(active);
      })
      .catch(() => {});
  }, [connection.workspaceReady]);

  const drainJob = async (jobId) => {
    setBusy(true); setError("");
    try {
      let current = job?.jobId === jobId ? job : null;
      while (!current || !["COMPLETED", "FAILED"].includes(current.status)) {
        const response = await axios.post(`${API_BASE}/api/campaigns/imports/${jobId}/process`, {}, { timeout: 300000 });
        current = response.data; setJob(current);
      }
      if (current.status === "COMPLETED") {
        onCreated(current.campaign);
        setFile(null);
        setForm((value) => ({ ...value, campaignName: "" }));
      }
    } catch (requestError) {
      setError(`${errorMessage(requestError)} The import is saved and can be resumed.`);
    } finally {
      setBusy(false);
    }
  };

  const submit = async (event) => {
    event.preventDefault(); setError("");
    if (!file) { setError("Choose a CSV, JSON, or JSONL lead file."); return; }
    if (!form.email && !form.phone) { setError("Select at least one lead channel."); return; }
    setBusy(true);
    try {
      const payload = new FormData();
      payload.append("file", file);
      payload.append("campaignName", form.campaignName);
      payload.append("industry", form.industry);
      payload.append("location", form.location);
      payload.append("channels", [form.email && "email", form.phone && "phone"].filter(Boolean).join(","));
      payload.append("chunkSize", String(form.chunkSize));
      const { data } = await axios.post(`${API_BASE}/api/campaigns/import`, payload, { timeout: 120000 });
      setJob(data); setBusy(false);
      await drainJob(data.jobId);
    } catch (requestError) {
      setError(errorMessage(requestError)); setBusy(false);
    }
  };

  const retry = async () => {
    if (!job?.jobId) return;
    setBusy(true); setError("");
    try {
      const { data } = await axios.post(`${API_BASE}/api/campaigns/imports/${job.jobId}/retry`, {}, { timeout: 30000 });
      setJob(data); setBusy(false); await drainJob(job.jobId);
    } catch (requestError) { setError(errorMessage(requestError)); setBusy(false); }
  };

  return (
    <form className="campaign-form upload-campaign" onSubmit={submit}>
      <div className="form-grid two">
        <label>Campaign name<input required value={form.campaignName} onChange={(event) => update({ campaignName: event.target.value })} placeholder="e.g. Uploaded Durban Leads" /></label>
        <label>Default industry<input required value={form.industry} onChange={(event) => update({ industry: event.target.value })} /></label>
      </div>
      <div className="form-grid two">
        <label>Default location<input required value={form.location} onChange={(event) => update({ location: event.target.value })} /></label>
        <label>Rows per chunk<input type="number" min="1" max="25" value={form.chunkSize} onChange={(event) => update({ chunkSize: Math.max(1, Math.min(25, Number(event.target.value) || 5)) })} /><small>Each row can create an email ticket, a call ticket, or both.</small></label>
      </div>
      <label className={`upload-dropzone ${file ? "selected" : ""}`}><Upload size={25} /><span><strong>{file?.name || "Choose lead data"}</strong><small>CSV, JSON, or JSONL · flexible Apify/Amplifier-style field names</small></span><input type="file" accept=".csv,.json,.jsonl,application/json,text/csv" onChange={(event) => setFile(event.target.files?.[0] || null)} /></label>
      <div className="channel-choice">
        <label className={form.email ? "checked" : ""}><input type="checkbox" checked={form.email} onChange={(event) => update({ email: event.target.checked })} /><Mail size={20} /><span><strong>Email leads</strong><small>Requires an email value</small></span></label>
        <label className={form.phone ? "checked" : ""}><input type="checkbox" checked={form.phone} onChange={(event) => update({ phone: event.target.checked })} /><Phone size={20} /><span><strong>Call leads</strong><small>Requires a phone value</small></span></label>
      </div>
      {job && <div className="import-progress"><div><strong>{job.fileName}</strong><StatusBadge status={job.status} /></div><div className="progress-track"><i style={{ width: `${job.progressPercent || 0}%` }} /></div><p>{Number(job.processedRows || 0).toLocaleString()} of {Number(job.totalRows || 0).toLocaleString()} rows processed · {job.succeededRows || 0} created · {job.skippedRows || 0} duplicates · {job.failedRows || 0} failed</p>{job.fileRetained && <small>The original file is retained until this job completes.</small>}</div>}
      {error && <div className="inline-alert danger">{error}</div>}
      <div className="form-submit">
        <button className="primary-button" type="submit" disabled={busy || !file || !connection.workspaceReady}>{busy ? <><RefreshCw className="spin" size={18} />Processing chunks…</> : <><Upload size={18} />Upload and create tickets</>}</button>
        {job && job.status !== "COMPLETED" && <button className="ghost-button" type="button" disabled={busy} onClick={job.status === "FAILED" ? retry : () => drainJob(job.jobId)}>{job.status === "FAILED" ? "Retry failed rows" : "Resume import"}</button>}
      </div>
    </form>
  );
}

function CampaignsPage({ presets, connection, onCreated }) {
  const [mode, setMode] = useState("discover");
  return (
    <div className="page-stack">
      <PageSection title="Create a lead campaign" eyebrow="Zendesk-first intake" description="Discover new leads or upload an existing lead file. Both paths create tagged Zendesk tickets and defer site generation.">
        <div className="campaign-mode-tabs"><button type="button" className={mode === "discover" ? "active" : ""} onClick={() => setMode("discover")}><MapPin size={17} />Find leads</button><button type="button" className={mode === "upload" ? "active" : ""} onClick={() => setMode("upload")}><Upload size={17} />Upload lead data</button></div>
        {mode === "discover" ? <CampaignForm presets={presets} connection={connection} onCreated={onCreated} /> : <UploadCampaignForm connection={connection} onCreated={onCreated} />}
      </PageSection>
      <div className="form-tag-grid">
        <article><Mail size={22} /><div><strong>Email form</strong><p>Business, contact email, location, source URL, campaign IDs, deploy checkbox, and email-send checkbox.</p><code>asf_form_email_lead</code></div></article>
        <article><Phone size={22} /><div><strong>Call form</strong><p>Business, phone number, location, source URL, campaign IDs, deploy checkbox, and call status.</p><code>asf_form_call_lead</code></div></article>
        <article><Webhook size={22} /><div><strong>Shared deployment</strong><p>Both channels can request a site. Existing artifacts and deployments are reused by canonical lead key.</p><code>asf_can_deploy</code></div></article>
      </div>
    </div>
  );
}

function LeadTable({ rows, channel, connection }) {
  if (!rows.length) return <EmptyState icon={channel === "email" ? Mail : Phone} title={`No ${channel === "email" ? "email" : "call"} leads`} text="This campaign did not return leads for this channel." />;
  return (
    <div className="lead-table-wrap">
      <table className="lead-table">
        <thead><tr><th>Business</th><th>{channel === "email" ? "Email contact" : "Phone contact"}</th><th>Source</th><th>Zendesk</th><th>Deploy request</th><th>Status</th></tr></thead>
        <tbody>{rows.map((row) => (
          <tr key={row.leadId}>
            <td><strong>{row.businessName}</strong><span>{row.contactName || "Contact name not supplied"}</span></td>
            <td><strong>{channel === "email" ? row.email : row.phone}</strong><span>{row.fields?.location || "No location"}</span></td>
            <td>{row.sourceUrl ? <a href={row.sourceUrl} target="_blank" rel="noreferrer">Listing <ExternalLink size={13} /></a> : <span>No URL</span>}</td>
            <td>{row.ticketId && connection.subdomain ? <a href={`https://${connection.subdomain}.zendesk.com/agent/tickets/${row.ticketId}`} target="_blank" rel="noreferrer">#{row.ticketId} <ExternalLink size={13} /></a> : <span>Local only</span>}</td>
            <td>{row.deployRequested ? <span className="yes"><Check size={14} />Requested</span> : <span className="waiting">Waiting on agent</span>}</td>
            <td><StatusBadge status={row.status} /></td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function LeadWorkspacePage({ campaigns, selectedCampaignId, setSelectedCampaignId, detail, connection, onSync, syncing }) {
  const [tab, setTab] = useState("email");
  return (
    <div className="page-stack">
      <PageSection
        title="Lead workspace"
        eyebrow="Channel queues"
        description="Email and call leads are stored separately and tagged for distinct Zendesk forms."
        action={<CampaignPicker campaigns={campaigns} selectedId={selectedCampaignId} onChange={setSelectedCampaignId} />}
      >
        {!detail ? <EmptyState icon={UsersRound} title="Select a campaign" text="Choose a campaign to inspect its channel-specific intake records." /> : <>
          <div className="campaign-context">
            <div><span>Campaign</span><strong>{detail.campaignName}</strong></div>
            <div><span>Industry</span><strong>{detail.industry}</strong></div>
            <div><span>Location</span><strong>{detail.location}</strong></div>
            <div><span>Apify batch</span><strong>{detail.batchId?.slice(0, 8) || "N/A"}</strong></div>
            {connection.connected && detail.metrics.zendeskTickets < detail.metrics.channelLeads && <button className="secondary-button" type="button" onClick={onSync} disabled={syncing}>{syncing ? "Syncing…" : "Sync unsent leads"}</button>}
          </div>
          <div className="channel-tabs">
            <button className={tab === "email" ? "active" : ""} type="button" onClick={() => setTab("email")}><Mail size={17} />Email leads <span>{detail.metrics.emailLeads}</span></button>
            <button className={tab === "phone" ? "active" : ""} type="button" onClick={() => setTab("phone")}><Phone size={17} />Call leads <span>{detail.metrics.callLeads}</span></button>
          </div>
          <LeadTable rows={tab === "email" ? detail.emailLeads : detail.callLeads} channel={tab} connection={connection} />
        </>}
      </PageSection>
      <PageSection title="Agent workflow" eyebrow="Zendesk automation" description="The ticket remains the operator interface; the site factory performs the heavy work behind the webhook.">
        <div className="workflow-steps">
          <article><i>1</i><TicketCheck size={20} /><strong>Review the lead</strong><p>Confirm the source, business details, and assigned agent.</p></article>
          <article><i>2</i><Check size={20} /><strong>Tick deploy</strong><p>The deploy_site webhook sends the approval, ticket, channel, and lead IDs.</p></article>
          <article><i>3</i><Zap size={20} /><strong>Generate once</strong><p>AI builds the HTML, then GitHub receives the artifact.</p></article>
          <article><i>4</i><Rocket size={20} /><strong>Receive the URL</strong><p>Netlify deploys and the same ticket gets a private comment with the link.</p></article>
        </div>
      </PageSection>
    </div>
  );
}

function DeploymentsPage({ campaigns, selectedCampaignId, setSelectedCampaignId, detail, history }) {
  const deployments = detail?.deployments || [];
  const live = deployments.filter((item) => ["DEPLOYED", "REUSED_DEPLOYMENT"].includes(item.status)).length;
  const failed = deployments.filter((item) => statusTone(item.status) === "danger").length;
  const pending = Math.max(0, deployments.length - live - failed);
  return (
    <div className="page-stack">
      <PageSection title="Deployment control" eyebrow="Campaign metrics" description="Track every deploy request, AI generation, repository, and live URL." action={<CampaignPicker campaigns={campaigns} selectedId={selectedCampaignId} onChange={setSelectedCampaignId} />}>
        {!detail ? <EmptyState icon={Rocket} title="Select a campaign" text="Choose a campaign to see deployment metrics." /> : <div className="deployment-summary">
          <DonutChart deployed={live} pending={pending} failed={failed} />
          <FunnelChart items={detail.funnel} />
        </div>}
      </PageSection>
      <PageSection title="Campaign deployment ledger" eyebrow="SQLite audit" description="One row per channel request, linked to its campaign and approval record.">
        {deployments.length ? <div className="deployment-grid">{deployments.map((item) => (
          <article className="deployment-card" key={item.deploymentId}>
            <div><span className={`channel-pill ${item.channel}`}>{item.channel === "email" ? <Mail size={14} /> : <Phone size={14} />}{item.channel}</span><StatusBadge status={item.status} /></div>
            <h3>{item.approvalId.slice(0, 12)}</h3>
            <dl><div><dt>AI generations</dt><dd>{item.aiGenerationCount}</dd></div><div><dt>Repo created</dt><dd>{item.repoCreated ? "Yes" : "No / reused"}</dd></div><div><dt>Requested</dt><dd>{formatDate(item.requestedAt)}</dd></div></dl>
            <div className="card-links">{item.repoUrl && <a href={item.repoUrl} target="_blank" rel="noreferrer"><GitBranch size={14} />Repository</a>}{item.liveUrl && <a href={item.liveUrl} target="_blank" rel="noreferrer"><ExternalLink size={14} />Live site</a>}</div>
            {item.error && <p className="card-error">{item.error}</p>}
          </article>
        ))}</div> : <EmptyState icon={CloudCog} title="No deployment rows" text="Campaign channel records appear here as soon as intake is created." />}
      </PageSection>
      <PageSection title="Recent provider deployments" eyebrow="Netlify history" description="Global deployment history retained by the existing pipeline registry.">
        {history.length ? <div className="history-list">{history.slice(0, 12).map((item) => <article key={item.id}><Rocket size={18} /><div><strong>{item.site_name || item.github_repo_full_name || "Generated site"}</strong><span>{formatDate(item.deployed_at)} · {item.publishMode || item.publish_mode}</span></div><StatusBadge status={item.state} />{item.url && <a href={item.url} target="_blank" rel="noreferrer"><ExternalLink size={15} /></a>}</article>)}</div> : <EmptyState icon={Rocket} title="Nothing deployed yet" text="Live Netlify builds appear here after an agent deploy request." />}
      </PageSection>
    </div>
  );
}

function ConnectionPanel({ connection, onConnected, onDisconnected }) {
  const [form, setForm] = useState({ subdomain: connection.subdomain || "", username: connection.username || "", apiToken: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => setForm((current) => ({ ...current, subdomain: connection.subdomain || "", username: connection.username || "" })), [connection.subdomain, connection.username]);
  const submit = async (event) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const { data } = await axios.put(`${API_BASE}/api/settings/zendesk-connection`, { ...form, validateConnection: true }, { timeout: 45000 });
      setForm((current) => ({ ...current, apiToken: "" })); onConnected(data);
    } catch (requestError) { setError(errorMessage(requestError)); } finally { setBusy(false); }
  };
  const disconnect = async () => {
    setBusy(true); setError("");
    try { const { data } = await axios.delete(`${API_BASE}/api/settings/zendesk-connection`); onDisconnected(data); }
    catch (requestError) { setError(errorMessage(requestError)); } finally { setBusy(false); }
  };
  return (
    <div className="connection-layout">
      <form className="connection-form" onSubmit={submit}>
        <label>Zendesk subdomain<div className="input-suffix"><input required value={form.subdomain} onChange={(event) => setForm({ ...form, subdomain: event.target.value })} placeholder="your-company" /><span>.zendesk.com</span></div></label>
        <label>Username / agent email<input required type="email" value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} placeholder="agent@company.com" /></label>
        <label>API token<input required={!connection.connected} type="password" value={form.apiToken} onChange={(event) => setForm({ ...form, apiToken: event.target.value })} placeholder={connection.connected ? "Enter a new token to reconnect" : "Paste Zendesk API token"} /></label>
        {error && <div className="inline-alert danger">{error}</div>}
        <div className="button-row"><button className="primary-button" disabled={busy || !form.apiToken} type="submit"><ShieldCheck size={17} />{busy ? "Checking…" : connection.connected ? "Reconnect" : "Connect Zendesk"}</button>{connection.connected && <button className="ghost-button danger" disabled={busy} type="button" onClick={disconnect}><Unplug size={17} />Disconnect session</button>}</div>
      </form>
      <aside className={`connection-status ${connection.connected ? "connected" : ""}`}>
        <div>{connection.connected ? <ShieldCheck size={28} /> : <CircleUserRound size={28} />}</div>
        <span>{connection.workspaceReady ? "Workspace ready" : connection.connected ? "Setup incomplete" : "Connection required"}</span>
        <h3>{connection.connected ? connection.subdomain : "Connect before campaigns"}</h3>
        <p>{connection.connected ? `${connection.username} · credentials from ${connection.source}${connection.workspaceReady ? " · fields and forms provisioned" : " · select a brand and provision below"}` : "Campaign creation, lead queues, and deployments stay locked until Zendesk is connected and provisioned."}</p>
        {connection.workspaceUrl && <a href={connection.workspaceUrl} target="_blank" rel="noreferrer">Open Zendesk <ExternalLink size={14} /></a>}
      </aside>
    </div>
  );
}

function ProvisionStatus({ status }) {
  const label = status === "ready" || status === "configured" ? "Ready" : status === "missing" || status === "planned" ? "Will create" : status === "conflict" ? "Conflict" : status;
  return <span className={`provision-status ${status || "planned"}`}>{status === "ready" || status === "configured" ? <Check size={12} /> : status === "conflict" ? <AlertTriangle size={12} /> : <Plus size={12} />}{label}</span>;
}

function ZendeskSetupWizard({ connection, onProvisioned }) {
  const defaultWebhookUrl = `${API_BASE}/api/zendesk/webhook`;
  const [setup, setSetup] = useState(null);
  const [config, setConfig] = useState({ emailFormName: "AI Site Factory - Email Lead", callFormName: "AI Site Factory - Call Lead", emailViewName: "AI Site Factory - Email Leads", callViewName: "AI Site Factory - Call Leads", deployedViewName: "AI Site Factory - Deployed Sites", webhookName: "AI Site Factory - Ticket actions", brandId: "", createViews: true, createAutomation: false, webhookUrl: defaultWebhookUrl });
  const [busy, setBusy] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const applySetup = useCallback((data) => {
    setSetup(data);
    setConfig((current) => {
      const savedWebhookUrl = data.config?.webhookUrl || "";
      let canonicalWebhookUrl = defaultWebhookUrl;
      if (savedWebhookUrl) {
        try { canonicalWebhookUrl = `${new URL(savedWebhookUrl).origin}/api/zendesk/webhook`; }
        catch { canonicalWebhookUrl = defaultWebhookUrl; }
      }
      return { ...current, ...(data.config || {}), brandId: data.config?.brandId || current.brandId || data.brands?.find((brand) => brand.default)?.id || "", webhookUrl: canonicalWebhookUrl };
    });
  }, [defaultWebhookUrl]);

  useEffect(() => {
    let active = true;
    axios.get(`${API_BASE}/api/settings/zendesk-setup`, { timeout: 20000 })
      .then(({ data }) => { if (active) applySetup(data); })
      .catch((requestError) => { if (active) setError(errorMessage(requestError)); });
    return () => { active = false; };
  }, [connection.connected, connection.subdomain, applySetup]);

  const changeConfig = (patch) => {
    setConfig((current) => ({ ...current, ...patch }));
    setConfirmed(false); setMessage("");
    setSetup((current) => current ? { ...current, inspected: false } : current);
  };
  const runSetup = async (mode) => {
    setBusy(mode); setError(""); setMessage("");
    try {
      const { data } = await axios.post(`${API_BASE}/api/settings/zendesk-setup/${mode}`, { ...config, brandId: config.brandId || null, confirm: mode === "provision" ? confirmed : false }, { timeout: mode === "provision" ? 300000 : 90000 });
      applySetup(data); setMessage(data.message || (mode === "inspect" ? "Instance inspected. Review the matches and planned resources below." : "Zendesk setup completed."));
      if (mode === "provision") {
        setConfirmed(false);
        const connectionResponse = await axios.get(`${API_BASE}/api/settings/zendesk-connection`, { timeout: 20000 });
        onProvisioned?.(connectionResponse.data);
      }
    } catch (requestError) { setError(errorMessage(requestError)); } finally { setBusy(""); }
  };

  const fields = setup?.fields || [];
  const forms = setup?.forms || [];
  const views = setup?.views || [];
  const automation = setup?.automation || [];
  const readyCount = (items) => items.filter((item) => ["ready", "configured"].includes(item.status)).length;
  const createCount = (items) => items.filter((item) => ["missing", "planned"].includes(item.status)).length;
  const conflicts = fields.filter((item) => item.status === "conflict");

  return <div className="setup-wizard">
    <div className="setup-disclaimer"><AlertTriangle size={22} /><div><strong>Review before provisioning</strong><p>This uses the connected administrator credentials to create or reconcile Zendesk configuration. It never deletes existing resources. Same-name fields with a different type stop the run before anything is created, and optional triggers are left inactive for an administrator to test and enable.</p></div></div>
    <div className="setup-sequence" aria-label="Provisioning order">{["Inspect instance", "Create fields", "Create forms", "Create views", "Stage automation"].map((label, index) => <span key={label}><i>{index + 1}</i>{label}</span>)}</div>
    <div className="setup-config-grid">
      <label>Email lead form name<input value={config.emailFormName} onChange={(event) => changeConfig({ emailFormName: event.target.value })} /></label>
      <label>Call lead form name<input value={config.callFormName} onChange={(event) => changeConfig({ callFormName: event.target.value })} /></label>
      <label>Brand assignment<select required value={config.brandId || ""} onChange={(event) => changeConfig({ brandId: event.target.value })}><option value="">Select an existing brand</option>{(setup?.brands || []).map((brand) => <option key={brand.id} value={brand.id}>{brand.name}{brand.default ? " (default)" : ""}</option>)}</select><small>The two forms, ticket routing, and managed views are tied to this existing Zendesk brand.</small></label>
      <label className="setup-toggle"><input type="checkbox" checked={config.createViews} onChange={(event) => changeConfig({ createViews: event.target.checked })} /><span><strong>Create managed views</strong><small>Email, call, and deployed queues filtered by stable tags.</small></span></label>
      <label className="setup-toggle"><input type="checkbox" checked={config.createAutomation} onChange={(event) => changeConfig({ createAutomation: event.target.checked })} /><span><strong>Stage webhook automation</strong><small>Creates an active authenticated webhook and inactive deploy/email triggers.</small></span></label>
      {config.createAutomation && <label>Public webhook URL<input type="url" value={config.webhookUrl || ""} onChange={(event) => changeConfig({ webhookUrl: event.target.value })} /><small>Must be public HTTPS. Localhost cannot receive Zendesk webhook calls.</small></label>}
    </div>
    {error && <div className="inline-alert danger">{error}</div>}
    {message && <div className="inline-alert success">{message}</div>}
    {!connection.connected && <div className="inline-alert warning">Connect Zendesk above before inspecting or provisioning this blueprint.</div>}
    <div className="setup-actions"><button className="ghost-button" type="button" disabled={!connection.connected || Boolean(busy)} onClick={() => runSetup("inspect")}><Eye size={17} />{busy === "inspect" ? "Inspecting…" : "Inspect instance"}</button><label className="confirm-change"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>I reviewed this plan and authorize these Zendesk configuration changes.</span></label><button className="primary-button" type="button" disabled={!connection.connected || !setup?.inspected || !config.brandId || !confirmed || Boolean(busy) || conflicts.length > 0} onClick={() => runSetup("provision")}><WandSparkles size={17} />{busy === "provision" ? "Provisioning in order…" : "Provision Zendesk setup"}</button></div>
    <div className="setup-summary-grid">
      <article><span>Ticket fields</span><strong>{fields.length}</strong><small>{readyCount(fields)} ready · {createCount(fields)} to create</small></article>
      <article><span>Channel forms</span><strong>{forms.length}</strong><small>{readyCount(forms)} ready · {createCount(forms)} to create</small></article>
      <article><span>Managed views</span><strong>{config.createViews ? views.length : 0}</strong><small>{config.createViews ? `${readyCount(views)} ready · ${createCount(views)} to create` : "Skipped by choice"}</small></article>
      <article><span>Automation</span><strong>{config.createAutomation ? automation.length : 0}</strong><small>{config.createAutomation ? "1 webhook · 3 inactive triggers" : "Documented, not provisioned"}</small></article>
    </div>
    <div className="setup-resource-block"><div><span>Preconfigured schema</span><h3>Fields and instance IDs</h3><p>IDs are discovered and saved by the app; they are shown here for confirmation, not manual entry.</p></div><div className="resource-table-wrap"><table className="resource-table"><thead><tr><th>Field</th><th>Type</th><th>Form</th><th>Status</th><th>Zendesk ID</th></tr></thead><tbody>{fields.map((field) => <tr key={field.key}><td><strong>{field.title}</strong><code>{field.key}</code></td><td>{field.type}</td><td>{field.forms.map((form) => form === "phone" ? "Call" : "Email").join(" + ")}</td><td><ProvisionStatus status={field.status} />{field.status === "conflict" && <small>Existing type: {field.existingType}</small>}{field.adaptedType && <small className="compatible-type">Reusing compatible {field.existingType} field</small>}</td><td><code>{field.resourceId || "Assigned during provisioning"}</code></td></tr>)}</tbody></table></div></div>
    <div className="setup-resource-cards">{[...forms, ...(config.createViews ? views : []), ...(config.createAutomation ? automation : [])].map((item) => <article key={item.key}><div>{item.type === "ticket_form" ? <TicketCheck size={18} /> : item.type === "view" ? <BarChart3 size={18} /> : <Webhook size={18} />}<ProvisionStatus status={item.status} /></div><strong>{item.name}</strong><span>{item.type.replace("ticket_form", "ticket form")}</span><code>{item.resourceId || "ID assigned after create"}</code>{item.type === "trigger" && <small>Created inactive for review</small>}</article>)}</div>
    <div className="setup-tag-contract"><div><strong>Tags used by forms, views, and trigger guards</strong><p>These values are stable and safe to use in your own Zendesk views, Explore reporting, and additional business rules.</p></div><div className="tag-cloud">{(setup?.tags || []).map((tag) => <code key={tag}>{tag}</code>)}</div></div>
  </div>;
}

function FieldMapping({ keys, fields, setFields, onSave, saving }) {
  const groups = [
    ["Campaign & identity", ["campaignId", "campaignName", "canonicalLeadKey", "pipelineId", "approvalId", "batchId"]],
    ["Business information", ["businessName", "contactName", "industry", "location", "address", "sourceUrl"]],
    ["Email form", ["contactEmail", "emailSendRequested"]],
    ["Call form", ["contactPhone", "phoneCallStatus"]],
    ["Automation", ["contactChannel", "leadStatus", "deployRequested", "liveUrl"]],
  ];
  const available = new Set(keys);
  return <div className="mapping-stack">{groups.map(([title, groupKeys]) => <fieldset key={title}><legend>{title}</legend><div className="mapping-grid">{groupKeys.filter((key) => available.has(key)).map((key) => <label key={key}>{FIELD_LABELS[key] || key}<input inputMode="numeric" value={fields[key] || ""} onChange={(event) => setFields((current) => ({ ...current, [key]: event.target.value }))} placeholder="Zendesk field ID" /><code>{key}</code></label>)}</div></fieldset>)}<div className="mapping-save"><button className="primary-button" type="button" onClick={onSave} disabled={saving}>{saving ? "Saving…" : "Save field mapping"}</button><span>Blank mappings are simply omitted from ticket payloads.</span></div></div>;
}

function ZendeskPage({ connection, setConnection }) {
  const webhookUrl = `${API_BASE}/api/zendesk/webhook`;
  return (
    <div className="page-stack">
      <PageSection title="Connect a Zendesk instance" eyebrow="Required workspace setup" description="Use a subdomain, agent username, and API token. Campaign work remains locked until this instance is provisioned.">
        <ConnectionPanel connection={connection} onConnected={setConnection} onDisconnected={setConnection} />
      </PageSection>
      <PageSection title="Provision the instance blueprint" eyebrow="Fields → forms → views → automation" description="Inspect the connected instance, review exact matches and missing resources, then provision the two channel workflows in dependency order.">
        <ZendeskSetupWizard connection={connection} onProvisioned={setConnection} />
      </PageSection>
      <PageSection title="Runtime contract" eyebrow="Two agent approvals" description="The provisioner can stage these rules, or your Zendesk administrator can build equivalent triggers using the displayed fields, forms, and tags.">
        <div className="webhook-grid">
          <article><div><Webhook size={20} /><strong>1. Deploy site</strong></div><p>The email and call forms each get an inactive trigger watching the Deploy site checkbox. The action uses a form-specific channel and a one-shot guard tag.</p></article>
          <article><div><Mail size={20} /><strong>2. Send approved email</strong></div><p>The email form gets a separate inactive trigger. It requires the deployed tag and the Send approved email checkbox, so it cannot run before a live URL exists.</p></article>
        </div>
        <div className="endpoint-strip"><span>POST</span><code>{webhookUrl}</code><span>Header</span><code>x-ai-site-factory-secret</code></div>
      </PageSection>
    </div>
  );
}

function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const [presets, setPresets] = useState(FALLBACK_PRESETS);
  const [campaigns, setCampaigns] = useState([]);
  const [totals, setTotals] = useState({ campaigns: 0, leads: 0, deployments: 0, pending: 0, aiGenerations: 0, reposCreated: 0 });
  const [selectedCampaignId, setSelectedCampaignIdState] = useState(() => localStorage.getItem("asf_campaign_id") || "");
  const [detail, setDetail] = useState(null);
  const [connection, setConnection] = useState({ connected: false, workspaceReady: false, setupStatus: "CONNECTION_REQUIRED", subdomain: "", username: "", source: "none" });
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState(null);
  const selectionInitialized = useRef(false);

  const setSelectedCampaignId = useCallback((value) => {
    setSelectedCampaignIdState(value);
    if (value) localStorage.setItem("asf_campaign_id", value); else localStorage.removeItem("asf_campaign_id");
  }, []);

  const loadDetail = useCallback(async (campaignId) => {
    if (!campaignId || !connection.workspaceReady) { setDetail(null); return; }
    try { const { data } = await axios.get(`${API_BASE}/api/campaigns/${campaignId}`, { timeout: 30000 }); setDetail(data); }
    catch (error) { setNotice({ tone: "danger", text: errorMessage(error) }); setDetail(null); }
  }, [connection.workspaceReady]);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    const requests = await Promise.allSettled([
      axios.get(`${API_BASE}/api/presets`, { timeout: 20000 }),
      axios.get(`${API_BASE}/api/campaigns?limit=100`, { timeout: 30000 }),
      axios.get(`${API_BASE}/api/settings/zendesk-connection`, { timeout: 20000 }),
    ]);
    if (requests[0].status === "fulfilled") setPresets(requests[0].value.data.presets || FALLBACK_PRESETS);
    if (requests[1].status === "fulfilled") {
      const data = requests[1].value.data; setCampaigns(data.campaigns || []); setTotals(data.totals || {});
      const selectedCampaign = data.campaigns?.find((campaign) => campaign.campaignId === selectedCampaignId);
      const populatedCampaign = data.campaigns?.find((campaign) => campaign.metrics?.channelLeads > 0);
      const shouldChooseDefault = !selectedCampaign || (!selectionInitialized.current && selectedCampaign.metrics?.channelLeads === 0 && populatedCampaign);
      if (shouldChooseDefault && data.campaigns?.length) {
        const defaultCampaign = populatedCampaign || data.campaigns[0];
        setSelectedCampaignId(defaultCampaign.campaignId);
      }
      selectionInitialized.current = true;
    }
    if (requests[2].status === "fulfilled") {
      const nextConnection = requests[2].value.data;
      setConnection(nextConnection);
      if (nextConnection.workspaceReady) {
        try {
          const historyResponse = await axios.get(`${API_BASE}/api/deployments/history?limit=100`, { timeout: 30000 });
          setHistory(historyResponse.data.deployments || []);
        } catch { setHistory([]); }
      } else {
        setHistory([]); setDetail(null);
      }
    }
    if (requests.every((item) => item.status === "rejected")) setNotice({ tone: "danger", text: "The backend could not be reached. Check that the API is running." });
    setLoading(false); setRefreshing(false);
  }, [selectedCampaignId, setSelectedCampaignId]);

  useEffect(() => { refresh(true); }, [refresh]);
  useEffect(() => { loadDetail(selectedCampaignId); }, [selectedCampaignId, loadDetail]);
  useEffect(() => {
    const timer = window.setInterval(() => { refresh(true); if (selectedCampaignId) loadDetail(selectedCampaignId); }, 20000);
    return () => window.clearInterval(timer);
  }, [refresh, selectedCampaignId, loadDetail]);

  const selectAndOpen = (campaignId) => {
    if (!connection.workspaceReady) {
      setNotice({ tone: "warning", text: "Finish the Zendesk brand, field, and form setup before opening campaign workspaces." });
      navigate("/zendesk"); return;
    }
    setSelectedCampaignId(campaignId); navigate("/leads");
  };
  const created = (campaign) => {
    setSelectedCampaignId(campaign.campaignId); setDetail(campaign);
    setNotice({ tone: "success", text: `${campaign.campaignName} created with ${campaign.metrics.channelLeads} channel records. No sites were generated yet.` });
    refresh(true); navigate("/leads");
  };
  const syncCampaign = async () => {
    if (!selectedCampaignId) return;
    setSyncing(true);
    try { const { data } = await axios.post(`${API_BASE}/api/campaigns/${selectedCampaignId}/sync-zendesk`, {}, { timeout: 300000 }); setDetail(data); setNotice({ tone: "success", text: `Synced ${data.sync?.synced || 0} lead tickets to Zendesk.` }); refresh(true); }
    catch (error) { setNotice({ tone: "danger", text: errorMessage(error) }); } finally { setSyncing(false); }
  };
  const pageName = NAV_ITEMS.find((item) => location.pathname.startsWith(item.to))?.label || "Overview";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div><Zap size={20} /></div><span><strong>AI Site Factory</strong><small>Lead-to-site campaigns</small></span></div>
        <nav>{NAV_ITEMS.map(({ to, label, icon: Icon, requiresSetup }) => requiresSetup && !connection.workspaceReady ? <button aria-label={`${label} locked`} className="nav-locked" key={to} type="button" onClick={() => navigate("/zendesk")}><Icon size={18} /><span>{label}</span><Lock size={12} /></button> : <NavLink aria-label={label} key={to} to={to}><Icon size={18} /><span>{label}</span></NavLink>)}</nav>
        <div className={`sidebar-connection ${connection.workspaceReady ? "connected" : ""}`}><i /> <div><span>{connection.workspaceReady ? "Zendesk workspace ready" : connection.connected ? "Zendesk setup incomplete" : "Zendesk required"}</span><small>{connection.connected ? connection.subdomain : "Campaigns locked"}</small></div></div>
      </aside>
      <main>
        <header className="topbar"><div><span>Workspace</span><h1>{pageName}</h1></div><div className="topbar-actions">{notice && <div className={`notice ${notice.tone}`}>{notice.text}<button type="button" onClick={() => setNotice(null)}>×</button></div>}<button className="refresh-button" type="button" onClick={() => refresh(false)} disabled={refreshing}><RefreshCw size={17} className={refreshing ? "spin" : ""} />Refresh</button></div></header>
        <div className="page-content">{loading ? <div className="loading-state"><RefreshCw className="spin" /><strong>Loading campaign workspace…</strong></div> : <Routes>
          <Route path="/overview" element={<OverviewPage campaigns={campaigns} totals={totals} onSelectCampaign={selectAndOpen} />} />
          <Route path="/campaigns" element={<SetupGuard connection={connection}><CampaignsPage presets={presets} connection={connection} onCreated={created} /></SetupGuard>} />
          <Route path="/leads" element={<SetupGuard connection={connection}><LeadWorkspacePage campaigns={campaigns} selectedCampaignId={selectedCampaignId} setSelectedCampaignId={setSelectedCampaignId} detail={detail} connection={connection} onSync={syncCampaign} syncing={syncing} /></SetupGuard>} />
          <Route path="/deployments" element={<SetupGuard connection={connection}><DeploymentsPage campaigns={campaigns} selectedCampaignId={selectedCampaignId} setSelectedCampaignId={setSelectedCampaignId} detail={detail} history={history} /></SetupGuard>} />
          <Route path="/zendesk" element={<ZendeskPage connection={connection} setConnection={setConnection} />} />
          <Route path="*" element={<Navigate to="/overview" replace />} />
        </Routes>}</div>
      </main>
    </div>
  );
}

export default function App() {
  return <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><AppShell /></BrowserRouter>;
}
