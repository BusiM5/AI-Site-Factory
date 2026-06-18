import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import axios from "axios";
import { gsap } from "gsap";
import "./App.css";

const API_BASE = "http://127.0.0.1:8000";
const MAX_UI_LOGS = 80;

const FALLBACK_PRESETS = [
  { id: "restaurants", label: "Restaurants", industry: "Restaurant", description: "Local restaurants and cafes." },
  { id: "plumbers", label: "Plumbers", industry: "Plumbing", description: "Local plumbing services." },
  { id: "dentists", label: "Dentists", industry: "Dental", description: "Dental practices and oral care providers." },
  { id: "beauty-salons", label: "Beauty Salons", industry: "Beauty", description: "Beauty salons, spas, and care studios." },
  { id: "gyms-fitness", label: "Gyms/Fitness", industry: "Fitness", description: "Gyms, trainers, and wellness studios." },
];

const FALLBACK_TEMPLATES = [
  { id: "default-service", name: "Default Service", description: "Clean landing page with hero, services, about, contact, and footer." },
  { id: "bold-local", name: "Bold Local", description: "High-contrast local business page." },
  { id: "premium-trust", name: "Premium Trust", description: "Polished trust-led page for professional services." },
];

const NAV_ITEMS = [
  { path: "/dashboard", label: "Dashboard" },
  { path: "/lead-discovery", label: "Lead Discovery" },
  { path: "/generate-approval", label: "Generate & Approval" },
  { path: "/deployments", label: "Deployments" },
  { path: "/pipeline-runs", label: "Pipeline Runs" },
  { path: "/admin", label: "Admin/Backend" },
  { path: "/settings", label: "Settings" },
];

function statusBadgeClass(status = "") {
  const value = String(status).toUpperCase();
  if (["READY", "APPROVED", "COMPLETED", "COMPLETED_REUSED", "VALID", "SUCCESS"].includes(value)) return "text-bg-success";
  if (["PENDING", "PENDING_APPROVAL", "PARTIAL_PENDING", "EXPORTING", "BUILDING", "ENQUEUED"].includes(value)) return "text-bg-warning";
  if (["FAILED", "EXPORT_FAILED", "DEPLOY_FAILED", "PUBLISH_FAILED", "INVALID", "ERROR"].includes(value)) return "text-bg-danger";
  return "text-bg-secondary";
}

function logBadgeClass(level = "") {
  const value = String(level).toLowerCase();
  if (["success", "info"].includes(value)) return value === "success" ? "text-bg-success" : "text-bg-info";
  if (["warning", "warn"].includes(value)) return "text-bg-warning";
  if (["danger", "error"].includes(value)) return "text-bg-danger";
  return "text-bg-secondary";
}

function displayDate(value) {
  if (!value) return "N/A";
  try {
    return new Date(value).toLocaleString();
  } catch (error) {
    return value;
  }
}

function deploymentGithubUrl(deployment) {
  return deployment?.github_repo_url || deployment?.githubRepoUrl || deployment?.githubExport?.repoUrl || deployment?.raw?.githubRepoUrl || deployment?.raw?.githubExport?.repoUrl;
}

function deploymentGithubName(deployment) {
  return deployment?.github_repo_full_name || deployment?.githubRepoFullName || deployment?.githubExport?.repository || deployment?.raw?.githubRepoFullName || deployment?.raw?.githubExport?.repository;
}

function Section({ title, action, children }) {
  return (
    <section className="panel entry-animate">
      <div className="panel-head">
        <h2>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function EmptyState({ title, text }) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      {text && <p>{text}</p>}
    </div>
  );
}

function AppShell() {
  const locationPath = useLocation();
  const contentRef = useRef(null);

  const [presets, setPresets] = useState(FALLBACK_PRESETS);
  const [templates, setTemplates] = useState(FALLBACK_TEMPLATES);
  const [selectedPresetId, setSelectedPresetId] = useState("restaurants");
  const [selectedTemplateId, setSelectedTemplateId] = useState("default-service");
  const [location, setLocation] = useState("Durban, South Africa");
  const [customQuery, setCustomQuery] = useState("");
  const [leadCount, setLeadCount] = useState(3);
  const [forceRefresh, setForceRefresh] = useState(false);
  const [forceRegenerate, setForceRegenerate] = useState(false);
  const [batchId, setBatchId] = useState(null);
  const [leads, setLeads] = useState([]);
  const [selectedLeadKeys, setSelectedLeadKeys] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [provinceStats, setProvinceStats] = useState({});
  const [duplicatesSkipped, setDuplicatesSkipped] = useState(0);
  const [lastDiscoveryCached, setLastDiscoveryCached] = useState(false);
  const [pipelineResult, setPipelineResult] = useState(null);
  const [reportingSummary, setReportingSummary] = useState(null);
  const [approvals, setApprovals] = useState([]);
  const [approvalPreviews, setApprovalPreviews] = useState({});
  const [deployments, setDeployments] = useState([]);
  const [pipelineRuns, setPipelineRuns] = useState([]);
  const [selectedRunDetail, setSelectedRunDetail] = useState(null);
  const [debugStatus, setDebugStatus] = useState(null);
  const [backendLogs, setBackendLogs] = useState([]);
  const [uiLogs, setUiLogs] = useState([]);
  const [apiProbe, setApiProbe] = useState(null);
  const [manualFlow, setManualFlow] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [running, setRunning] = useState(false);
  const [approvalBusy, setApprovalBusy] = useState(null);
  const [debugBusy, setDebugBusy] = useState(null);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState("info");

  const addUiLog = useCallback((level, event, text, details = {}) => {
    const entry = { id: `${Date.now()}-${Math.random()}`, timestamp: new Date().toISOString(), level, event, message: text, details };
    setUiLogs((current) => [entry, ...current].slice(0, MAX_UI_LOGS));
  }, []);

  const formatApiError = useCallback((error) => {
    const detail = error.response?.data?.detail;
    return {
      status: error.response?.status || "NETWORK",
      requestId: error.response?.headers?.["x-request-id"],
      message: typeof detail === "string" ? detail : detail?.message || error.message || "Unknown API failure",
    };
  }, []);

  const setNotice = useCallback((text, tone = "info") => {
    setMessage(text);
    setMessageTone(tone);
  }, []);

  const callApi = useCallback(
    async (label, method, path, data, options = {}) => {
      addUiLog("info", "api.start", `${label} started.`, { method, path });
      try {
        const response = await axios({ method, url: `${API_BASE}${path}`, data, timeout: options.timeout || 180000 });
        addUiLog("success", "api.success", `${label} succeeded.`, { method, path, status: response.status, requestId: response.headers?.["x-request-id"] });
        return response.data;
      } catch (error) {
        const failure = formatApiError(error);
        addUiLog("danger", "api.failure", `${label} failed: ${failure.message}`, { method, path, ...failure });
        throw error;
      }
    },
    [addUiLog, formatApiError]
  );

  const refreshDiagnostics = useCallback(
    async (silent = false) => {
      try {
        const [statusResponse, logsResponse] = await Promise.all([
          axios.get(`${API_BASE}/api/debug/status`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/debug/logs?limit=80`, { timeout: 15000 }),
        ]);
        setDebugStatus(statusResponse.data);
        setBackendLogs(logsResponse.data.logs || []);
        if (!silent) addUiLog("success", "debug.refresh", "Diagnostics refreshed.", { status: statusResponse.data.status });
      } catch (error) {
        if (!silent) {
          const failure = formatApiError(error);
          addUiLog("danger", "debug.refresh_failed", `Diagnostics failed: ${failure.message}`, failure);
        }
      }
    },
    [addUiLog, formatApiError]
  );

  const refreshOperations = useCallback(
    async (silent = false) => {
      try {
        const [summaryResponse, approvalsResponse, deploymentsResponse, runsResponse] = await Promise.all([
          axios.get(`${API_BASE}/api/reporting/summary`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/approvals?status=ALL&limit=80`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/deployments/history?limit=80`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/pipeline/runs?limit=40`, { timeout: 15000 }),
        ]);
        setReportingSummary(summaryResponse.data);
        setApprovals(approvalsResponse.data.approvals || []);
        setDeployments(deploymentsResponse.data.deployments || []);
        setPipelineRuns(runsResponse.data.runs || []);
        if (!silent) addUiLog("success", "operations.refresh", "Pipeline reporting refreshed.", { pendingApprovals: summaryResponse.data.metrics?.pendingApprovals || 0 });
      } catch (error) {
        if (!silent) {
          const failure = formatApiError(error);
          addUiLog("danger", "operations.refresh_failed", `Reporting refresh failed: ${failure.message}`, failure);
        }
      }
    },
    [addUiLog, formatApiError]
  );

  useEffect(() => {
    const loadConfig = async () => {
      try {
        const [presetResponse, templateResponse] = await Promise.all([
          callApi("Preset API", "get", "/api/presets", null, { timeout: 15000 }),
          callApi("Template API", "get", "/api/templates", null, { timeout: 15000 }),
        ]);
        setPresets(presetResponse.presets || FALLBACK_PRESETS);
        setTemplates(templateResponse.templates || FALLBACK_TEMPLATES);
        setNotice("Backend configuration loaded.", "success");
      } catch (error) {
        setNotice("Backend configuration could not be loaded.", "warning");
      }
    };

    loadConfig();
    refreshDiagnostics(true);
    refreshOperations(true);
  }, [callApi, refreshDiagnostics, refreshOperations, setNotice]);

  useEffect(() => {
    const interval = setInterval(() => {
      refreshDiagnostics(true);
      refreshOperations(true);
    }, 7000);
    return () => clearInterval(interval);
  }, [refreshDiagnostics, refreshOperations]);

  useEffect(() => {
    if (!contentRef.current) return;
    try {
      gsap.fromTo(contentRef.current.querySelectorAll(".entry-animate"), { y: 12, opacity: 0 }, { y: 0, opacity: 1, duration: 0.42, ease: "power2.out", stagger: 0.04 });
    } catch (error) {
      addUiLog("warning", "animation.skipped", "Entry animation skipped.", { reason: error.message });
    }
  }, [addUiLog, locationPath.pathname]);

  const selectedPreset = useMemo(() => presets.find((preset) => preset.id === selectedPresetId) || presets[0], [presets, selectedPresetId]);
  const selectedTemplate = useMemo(() => templates.find((template) => template.id === selectedTemplateId) || templates[0], [templates, selectedTemplateId]);
  const selectedLeads = useMemo(() => leads.filter((lead) => selectedLeadKeys.includes(lead.leadKey)), [leads, selectedLeadKeys]);
  const pendingApprovals = approvals.filter((approval) => ["PENDING", "EXPORT_FAILED", "EXPORTING"].includes(approval.status));

  const toggleLead = (leadKey) => {
    setSelectedLeadKeys((current) => (current.includes(leadKey) ? current.filter((key) => key !== leadKey) : [...current, leadKey]));
  };

  const discoverLeads = async () => {
    if (!selectedPreset?.id) {
      setNotice("Select a business type first.", "warning");
      return;
    }
    try {
      setDiscovering(true);
      setPipelineResult(null);
      setSelectedLeadKeys([]);
      setWarnings([]);
      setNotice("Searching selected location with Apify...", "info");
      const data = await callApi(
        "Lead Discovery API",
        "post",
        "/api/leads/discover",
        {
          presetId: selectedPreset.id,
          location: location || "Durban, South Africa",
          query: customQuery || null,
          limit: leadCount,
          forceRefresh,
        },
        { timeout: 600000 }
      );
      setBatchId(data.batchId);
      setLeads(data.leads || []);
      setWarnings(data.warnings || []);
      setProvinceStats(data.provinceStats || {});
      setDuplicatesSkipped(data.duplicatesSkipped || 0);
      setLastDiscoveryCached(Boolean(data.cached));
      setNotice(`${data.cached ? "Loaded cached" : "Fetched"} ${data.leads?.length || 0} leads.`, data.leads?.length ? "success" : "warning");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setLeads([]);
      setNotice(failure.message || "Lead discovery failed.", "danger");
    } finally {
      setDiscovering(false);
    }
  };

  const runPipeline = async () => {
    if (!selectedLeads.length) {
      setNotice("Select one or more leads.", "warning");
      return;
    }
    try {
      setRunning(true);
      setPipelineResult(null);
      setNotice("Generating pages and exporting repositories...", "info");
      const data = await callApi(
        "Full Pipeline API",
        "post",
        "/api/pipeline/run",
        {
          sourceBatchId: batchId,
          templateId: selectedTemplate.id,
          leads: selectedLeads,
          resumeExisting: true,
          forceRegenerate,
        },
        { timeout: 600000 }
      );
      setPipelineResult(data);
      setNotice(`Pipeline finished with status ${data.status}.`, data.status === "FAILED" ? "danger" : data.status.includes("PENDING") ? "warning" : "success");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Pipeline run failed.", "danger");
    } finally {
      setRunning(false);
    }
  };

  const previewApproval = async (approvalId) => {
    if (approvalPreviews[approvalId]) {
      setApprovalPreviews((current) => ({ ...current, [approvalId]: null }));
      return;
    }
    try {
      const data = await callApi("Approval Preview API", "get", `/api/approvals/${approvalId}?includeHtml=true`);
      setApprovalPreviews((current) => ({ ...current, [approvalId]: data.pendingPreviewHtml || "" }));
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Preview failed.", "danger");
    }
  };

  const approveSite = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      const data = await callApi("Approval Deploy API", "post", `/api/approvals/${approvalId}/approve`, { approvedBy: "Dashboard Operator", notes: "Approved from dashboard." }, { timeout: 420000 });
      setNotice(`Approved and deployed ${data.businessName}.`, data.status === "APPROVED" ? "success" : "warning");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Approval deployment failed.", "danger");
      refreshOperations(true);
    } finally {
      setApprovalBusy(null);
    }
  };

  const retryExport = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      const data = await callApi("GitHub Export Retry API", "post", `/api/approvals/${approvalId}/retry-export`, { requestedBy: "Dashboard Operator" }, { timeout: 240000 });
      setNotice(`GitHub export ready for ${data.businessName}.`, "success");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "GitHub export retry failed.", "danger");
    } finally {
      setApprovalBusy(null);
    }
  };

  const rejectSite = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      const data = await callApi("Approval Reject API", "post", `/api/approvals/${approvalId}/reject`, { rejectedBy: "Dashboard Operator", reason: "Rejected from dashboard." });
      setNotice(`Rejected ${data.businessName}.`, "warning");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Reject failed.", "danger");
    } finally {
      setApprovalBusy(null);
    }
  };

  const regenerateSite = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      const data = await callApi("Approval Regenerate API", "post", `/api/approvals/${approvalId}/regenerate`, { requestedBy: "Dashboard Operator" }, { timeout: 600000 });
      setNotice(`Regenerated ${data.businessName}.`, data.status === "EXPORT_FAILED" ? "danger" : "success");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Regenerate failed.", "danger");
    } finally {
      setApprovalBusy(null);
    }
  };

  const runApiProbe = async (includeExternal = false) => {
    try {
      setDebugBusy(includeExternal ? "external-probe" : "local-probe");
      const data = await callApi("API Probe", "post", "/api/debug/probe", { includeExternal, checks: [] }, { timeout: includeExternal ? 120000 : 30000 });
      setApiProbe(data);
      setNotice(data.status === "VALID" ? "API validation passed." : "API validation found failures.", data.status === "VALID" ? "success" : "danger");
      refreshDiagnostics(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "API probe failed.", "danger");
    } finally {
      setDebugBusy(null);
    }
  };

  const runManualFlow = async () => {
    try {
      setDebugBusy("manual-flow");
      const scrape = await callApi("Scrape API", "post", "/api/scrape/lead", { url: "https://example.com" });
      const intake = await callApi("Lead Intake API", "post", "/api/leads/intake", { rawLeadRow: { businessName: scrape.businessName, email: scrape.email, domain: scrape.domain || "example.com", category: scrape.category || "General Services", location: scrape.location || "South Africa", notes: scrape.notes || "Sample lead." }, sourceType: "ui-debugger" });
      const clean = await callApi("Lead Clean API", "post", `/api/leads/${intake.leadId}/clean`);
      const content = await callApi("Content Generate API", "post", "/api/content/generate", { leadRecord: clean });
      const preview = await callApi("Preview Build API", "post", "/api/site/build-preview", { leadId: clean.leadId, contentPacket: content.contentPacket, deployMode: "preview" });
      await callApi("Lead Lookup API", "get", `/api/leads/${intake.leadId}`);
      await callApi("Outreach Generate API", "post", "/api/outreach/generate", { leadId: intake.leadId, businessName: clean.businessName, email: clean.email, category: clean.category, previewReference: preview.previewUrl });
      setManualFlow({ leadId: intake.leadId, previewUrl: preview.previewUrl, steps: ["scrape", "intake", "clean", "content", "preview", "lookup", "outreach"] });
      setNotice("Safe local API flow test passed.", "success");
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Safe flow test failed.", "danger");
    } finally {
      setDebugBusy(null);
    }
  };

  const loadRunDetail = async (pipelineId) => {
    try {
      const data = await callApi("Pipeline Run Detail API", "get", `/api/pipeline/runs/${pipelineId}`, null, { timeout: 15000 });
      setSelectedRunDetail(data);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Pipeline run detail failed.", "danger");
    }
  };

  const shared = {
    presets,
    templates,
    selectedPresetId,
    setSelectedPresetId,
    selectedTemplateId,
    setSelectedTemplateId,
    selectedPreset,
    selectedTemplate,
    location,
    setLocation,
    customQuery,
    setCustomQuery,
    leadCount,
    setLeadCount,
    forceRefresh,
    setForceRefresh,
    forceRegenerate,
    setForceRegenerate,
    leads,
    selectedLeadKeys,
    selectedLeads,
    toggleLead,
    setSelectedLeadKeys,
    warnings,
    provinceStats,
    duplicatesSkipped,
    lastDiscoveryCached,
    batchId,
    pipelineResult,
    reportingSummary,
    approvals,
    pendingApprovals,
    approvalPreviews,
    deployments,
    pipelineRuns,
    selectedRunDetail,
    debugStatus,
    backendLogs,
    uiLogs,
    apiProbe,
    manualFlow,
    discovering,
    running,
    approvalBusy,
    debugBusy,
    discoverLeads,
    runPipeline,
    previewApproval,
    approveSite,
    retryExport,
    rejectSite,
    regenerateSite,
    runApiProbe,
    runManualFlow,
    refreshDiagnostics,
    refreshOperations,
    loadRunDetail,
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <strong>AI Site Factory</strong>
          <span>GitHub to Netlify</span>
        </div>
        <nav>
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.path} to={item.path} className={({ isActive }) => (isActive ? "active" : "")}>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="content-shell" ref={contentRef}>
        <header className="topbar">
          <div>
            <h1>{NAV_ITEMS.find((item) => item.path === locationPath.pathname)?.label || "Dashboard"}</h1>
            <span className={`badge ${statusBadgeClass(debugStatus?.status)}`}>{debugStatus?.status || "LOADING"}</span>
          </div>
          {message && <div className={`notice ${messageTone}`}>{message}</div>}
        </header>
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<DashboardPage {...shared} />} />
          <Route path="/lead-discovery" element={<LeadDiscoveryPage {...shared} />} />
          <Route path="/generate-approval" element={<GenerateApprovalPage {...shared} />} />
          <Route path="/deployments" element={<DeploymentsPage {...shared} />} />
          <Route path="/pipeline-runs" element={<PipelineRunsPage {...shared} />} />
          <Route path="/admin" element={<AdminPage {...shared} />} />
          <Route path="/settings" element={<SettingsPage {...shared} />} />
        </Routes>
      </main>
    </div>
  );
}

function DashboardPage({ reportingSummary, pendingApprovals, deployments, pipelineRuns, backendLogs, uiLogs }) {
  const metrics = reportingSummary?.metrics || {};
  return (
    <div className="page-grid">
      <section className="metric-grid entry-animate">
        <div className="metric-card"><span>{metrics.leadsDiscovered || 0}</span><label>Leads</label></div>
        <div className="metric-card"><span>{metrics.githubRepos || 0}</span><label>Repos</label></div>
        <div className="metric-card"><span>{metrics.gitDeployments || metrics.approvedDeployments || 0}</span><label>Deploys</label></div>
        <div className="metric-card"><span>{pendingApprovals.length}</span><label>Approvals</label></div>
      </section>
      <Section title="Recent Pipeline Runs">
        <RunList runs={pipelineRuns.slice(0, 6)} compact />
      </Section>
      <Section title="Deployed Sites">
        <DeploymentList deployments={deployments.slice(0, 6)} />
      </Section>
      <Section title="Live Logs">
        <LogColumns uiLogs={uiLogs} backendLogs={backendLogs} />
      </Section>
    </div>
  );
}

function LeadDiscoveryPage(props) {
  const { presets, selectedPresetId, setSelectedPresetId, location, setLocation, customQuery, setCustomQuery, leadCount, setLeadCount, forceRefresh, setForceRefresh, leads, selectedLeadKeys, setSelectedLeadKeys, toggleLead, discovering, discoverLeads, warnings, duplicatesSkipped, lastDiscoveryCached, provinceStats } = props;
  return (
    <div className="page-grid">
      <Section
        title="Lead Discovery"
        action={<button className="btn btn-primary" type="button" onClick={discoverLeads} disabled={discovering}>{discovering ? "Searching..." : "Search Leads"}</button>}
      >
        <div className="preset-grid">
          {presets.map((preset) => (
            <button key={preset.id} className={`preset-tile ${selectedPresetId === preset.id ? "selected" : ""}`} type="button" onClick={() => setSelectedPresetId(preset.id)}>
              <strong>{preset.label}</strong>
              <span>{preset.industry}</span>
            </button>
          ))}
        </div>
        <div className="control-grid mt-3">
          <label>Location<input className="form-control" value={location} onChange={(event) => setLocation(event.target.value)} /></label>
          <label>Query<input className="form-control" value={customQuery} onChange={(event) => setCustomQuery(event.target.value)} placeholder="Optional" /></label>
          <label>Lead count<select className="form-select" value={leadCount} onChange={(event) => setLeadCount(Number(event.target.value))}>{[1, 2, 3, 4, 5].map((count) => <option key={count} value={count}>{count}</option>)}</select></label>
          <label className="checkline"><input type="checkbox" checked={forceRefresh} onChange={(event) => setForceRefresh(event.target.checked)} />Force refresh</label>
        </div>
        <div className="status-strip">
          <span className={`badge ${lastDiscoveryCached ? "text-bg-info" : "text-bg-success"}`}>{lastDiscoveryCached ? "CACHE" : "LIVE"}</span>
          <span>{duplicatesSkipped} duplicates skipped</span>
          {Object.entries(provinceStats).map(([key, value]) => <span key={key}>{key}: {value.selected || 0}</span>)}
        </div>
        {warnings.map((warning) => <div className="alert alert-warning" key={warning}>{warning}</div>)}
      </Section>
      <Section
        title="Discovered Leads"
        action={
          <div className="button-row">
            <button className="btn btn-outline-secondary" type="button" onClick={() => setSelectedLeadKeys(leads.map((lead) => lead.leadKey))} disabled={!leads.length}>Select All</button>
            <button className="btn btn-outline-secondary" type="button" onClick={() => setSelectedLeadKeys([])} disabled={!selectedLeadKeys.length}>Clear</button>
          </div>
        }
      >
        <LeadTable leads={leads} selectedLeadKeys={selectedLeadKeys} toggleLead={toggleLead} />
      </Section>
    </div>
  );
}

function LeadTable({ leads, selectedLeadKeys, toggleLead }) {
  if (!leads.length) return <EmptyState title="No leads loaded" text="Run a search from the selected location." />;
  return (
    <div className="table-responsive data-table-wrap">
      <table className="table align-middle">
        <thead>
          <tr><th></th><th>Business</th><th>Contact</th><th>Location</th><th>Category</th><th>Rating</th><th>Source</th></tr>
        </thead>
        <tbody>
          {leads.map((lead) => (
            <tr key={lead.leadKey}>
              <td><input aria-label={`Select ${lead.businessName}`} type="checkbox" checked={selectedLeadKeys.includes(lead.leadKey)} onChange={() => toggleLead(lead.leadKey)} /></td>
              <td><strong>{lead.businessName}</strong><span>{lead.address || lead.domain || ""}</span></td>
              <td><span>{lead.email || "No email"}</span><span>{lead.phone || lead.website || ""}</span></td>
              <td>{lead.location || "N/A"}</td>
              <td>{lead.category}</td>
              <td>{lead.rating ? `${lead.rating} (${lead.reviewsCount || 0})` : "N/A"}</td>
              <td>{lead.sourceUrl ? <a href={lead.sourceUrl} target="_blank" rel="noreferrer">Open</a> : lead.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GenerateApprovalPage(props) {
  const { templates, selectedTemplateId, setSelectedTemplateId, selectedLeads, forceRegenerate, setForceRegenerate, running, runPipeline, pipelineResult, approvals, approvalPreviews, approvalBusy, previewApproval, approveSite, retryExport, rejectSite, regenerateSite } = props;
  return (
    <div className="page-grid">
      <Section
        title="Generate & Approval"
        action={<button className="btn btn-primary" type="button" onClick={runPipeline} disabled={running || !selectedLeads.length}>{running ? "Running..." : "Run Pipeline"}</button>}
      >
        <div className="control-grid">
          <label>Template<select className="form-select" value={selectedTemplateId} onChange={(event) => setSelectedTemplateId(event.target.value)}>{templates.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}</select></label>
          <label className="checkline"><input type="checkbox" checked={forceRegenerate} onChange={(event) => setForceRegenerate(event.target.checked)} />Force regenerate</label>
          <div className="stat-box"><strong>{selectedLeads.length}</strong><span>Selected leads</span></div>
        </div>
      </Section>
      <Section title="Pipeline Results">
        {pipelineResult?.results?.length ? (
          <div className="result-grid">
            {pipelineResult.results.map((result) => <PipelineResultCard key={result.leadKey} result={result} />)}
          </div>
        ) : (
          <EmptyState title="No pipeline output" text="Select leads and run the pipeline." />
        )}
      </Section>
      <Section title="Approval Queue">
        {approvals.length ? (
          <div className="approval-list">
            {approvals.map((approval) => (
              <ApprovalItem
                key={approval.approvalId}
                approval={approval}
                previewHtml={approvalPreviews[approval.approvalId]}
                busy={approvalBusy === approval.approvalId}
                onPreview={() => previewApproval(approval.approvalId)}
                onApprove={() => approveSite(approval.approvalId)}
                onRetry={() => retryExport(approval.approvalId)}
                onReject={() => rejectSite(approval.approvalId)}
                onRegenerate={() => regenerateSite(approval.approvalId)}
              />
            ))}
          </div>
        ) : (
          <EmptyState title="No approvals" text="Generated pages appear here." />
        )}
      </Section>
    </div>
  );
}

function PipelineResultCard({ result }) {
  return (
    <article className="result-card">
      <div className="item-head">
        <h3>{result.businessName}</h3>
        <span className={`badge ${statusBadgeClass(result.status)}`}>{result.status}</span>
      </div>
      <dl>
        <div><dt>Approval</dt><dd>{result.approvalStatus || "N/A"} {result.pendingApprovalId ? `| ${result.pendingApprovalId}` : ""}</dd></div>
        <div><dt>GitHub</dt><dd>{result.githubExport?.repoUrl ? <a href={result.githubExport.repoUrl} target="_blank" rel="noreferrer">{result.githubExport.repository}</a> : "No repository"}</dd></div>
        <div><dt>Commit</dt><dd>{result.githubExport?.commitSha || "N/A"}</dd></div>
      </dl>
      {result.stepHistory?.length > 0 && <StepList steps={result.stepHistory} />}
      {result.errors?.length > 0 && <div className="alert alert-danger">{result.errors.join(" ")}</div>}
    </article>
  );
}

function ApprovalItem({ approval, previewHtml, busy, onPreview, onApprove, onRetry, onReject, onRegenerate }) {
  const canApprove = approval.status === "PENDING" && approval.githubExport?.repoUrl;
  return (
    <article className="approval-item">
      <div className="item-head">
        <div><h3>{approval.businessName}</h3><span>{approval.approvalId}</span></div>
        <span className={`badge ${statusBadgeClass(approval.status)}`}>{approval.status}</span>
      </div>
      <dl>
        <div><dt>GitHub repo</dt><dd>{approval.githubExport?.repoUrl ? <a href={approval.githubExport.repoUrl} target="_blank" rel="noreferrer">{approval.githubExport.repository}</a> : "Pending export"}</dd></div>
        <div><dt>Commit</dt><dd>{approval.githubExport?.commitSha || "N/A"}</dd></div>
        <div><dt>Created</dt><dd>{displayDate(approval.createdAt)}</dd></div>
      </dl>
      <div className="button-row">
        <button className="btn btn-outline-secondary btn-sm" type="button" onClick={onPreview} disabled={busy || !approval.previewAvailable}>{previewHtml ? "Hide Preview" : "Preview"}</button>
        {approval.status === "EXPORT_FAILED" && <button className="btn btn-outline-primary btn-sm" type="button" onClick={onRetry} disabled={busy}>Retry Export</button>}
        <button className="btn btn-success btn-sm" type="button" onClick={onApprove} disabled={busy || !canApprove}>{busy ? "Working..." : "Approve"}</button>
        <button className="btn btn-outline-primary btn-sm" type="button" onClick={onRegenerate} disabled={busy}>Regenerate</button>
        <button className="btn btn-outline-danger btn-sm" type="button" onClick={onReject} disabled={busy || !["PENDING", "EXPORT_FAILED"].includes(approval.status)}>Reject</button>
      </div>
      {previewHtml && <iframe className="approval-preview" title={`Preview ${approval.businessName}`} srcDoc={previewHtml} />}
      {approval.errors?.length > 0 && <pre>{JSON.stringify(approval.errors, null, 2)}</pre>}
    </article>
  );
}

function DeploymentsPage({ deployments }) {
  return (
    <Section title="Deployment History">
      <DeploymentList deployments={deployments} detailed />
    </Section>
  );
}

function DeploymentList({ deployments, detailed = false }) {
  if (!deployments.length) return <EmptyState title="No deployments" text="Approved sites appear here." />;
  return (
    <div className="deployment-list">
      {deployments.map((deployment) => (
        <article className="deployment-item" key={deployment.id || deployment.deployId}>
          <div className="item-head">
            <div><h3>{deployment.site_name || deployment.siteName || deploymentGithubName(deployment) || "Netlify site"}</h3><span>{deployment.pipeline_id || deployment.pipelineId || "No pipeline ID"}</span></div>
            <span className={`badge ${statusBadgeClass(deployment.state)}`}>{deployment.state || "unknown"}</span>
          </div>
          <dl>
            <div><dt>Netlify URL</dt><dd>{deployment.url ? <a href={deployment.url} target="_blank" rel="noreferrer">{deployment.url}</a> : "N/A"}</dd></div>
            <div><dt>GitHub repo</dt><dd>{deploymentGithubUrl(deployment) ? <a href={deploymentGithubUrl(deployment)} target="_blank" rel="noreferrer">{deploymentGithubName(deployment)}</a> : "N/A"}</dd></div>
            <div><dt>Build</dt><dd>{deployment.build_id || deployment.buildId || deployment.raw?.buildId || "N/A"}</dd></div>
            {detailed && <div><dt>Approved</dt><dd>{displayDate(deployment.deployed_at || deployment.deployedAt)}</dd></div>}
          </dl>
          {deployment.raw?.errors && <pre>{JSON.stringify(deployment.raw.errors, null, 2)}</pre>}
        </article>
      ))}
    </div>
  );
}

function PipelineRunsPage({ pipelineRuns, selectedRunDetail, loadRunDetail }) {
  const selectedRun = selectedRunDetail?.run;
  return (
    <div className="split-grid">
      <Section title="Pipeline Runs">
        <RunList runs={pipelineRuns} onSelect={loadRunDetail} />
      </Section>
      <Section title="Run Detail">
        {selectedRun ? (
          <div className="detail-stack">
            <div className="item-head"><h3>{selectedRun.pipeline_id}</h3><span className={`badge ${statusBadgeClass(selectedRun.status)}`}>{selectedRun.status}</span></div>
            <StepList steps={selectedRunDetail.steps || []} />
            <div className="approval-list">{(selectedRunDetail.approvals || []).map((approval) => <ApprovalItem key={approval.approvalId} approval={approval} previewHtml={null} busy={false} onPreview={() => {}} onApprove={() => {}} onRetry={() => {}} onReject={() => {}} onRegenerate={() => {}} />)}</div>
          </div>
        ) : (
          <EmptyState title="No run selected" text="Open a run from the list." />
        )}
      </Section>
    </div>
  );
}

function RunList({ runs, onSelect, compact = false }) {
  if (!runs.length) return <EmptyState title="No pipeline runs" />;
  return (
    <div className="run-list">
      {runs.map((run) => (
        <button key={run.pipeline_id} type="button" className="run-row" onClick={() => onSelect?.(run.pipeline_id)}>
          <span className={`badge ${statusBadgeClass(run.status)}`}>{run.status}</span>
          <strong>{run.pipeline_id}</strong>
          {!compact && <span>{run.pending_count || 0} pending | {run.completed_count || 0} complete | {run.failed_count || 0} failed</span>}
        </button>
      ))}
    </div>
  );
}

function StepList({ steps }) {
  if (!steps?.length) return null;
  return (
    <details open>
      <summary>Step history</summary>
      <div className="step-list">
        {steps.map((step, index) => (
          <div className="step-row" key={`${step.step}-${index}`}>
            <span className={`badge ${statusBadgeClass(step.status)}`}>{step.status}</span>
            <strong>{step.step}</strong>
            <span>{step.provider || "local"} | {step.durationMs || step.duration_ms || 0} ms</span>
          </div>
        ))}
      </div>
    </details>
  );
}

function AdminPage({ debugStatus, backendLogs, uiLogs, apiProbe, debugBusy, runApiProbe, runManualFlow, manualFlow, refreshDiagnostics }) {
  return (
    <div className="page-grid">
      <Section
        title="API Safety Center"
        action={<div className="button-row"><button className="btn btn-outline-secondary" type="button" onClick={() => refreshDiagnostics(false)}>Refresh</button><button className="btn btn-primary" type="button" onClick={() => runApiProbe(false)} disabled={Boolean(debugBusy)}>Local Probe</button><button className="btn btn-outline-primary" type="button" onClick={() => runApiProbe(true)} disabled={Boolean(debugBusy)}>External Probe</button><button className="btn btn-outline-secondary" type="button" onClick={runManualFlow} disabled={Boolean(debugBusy)}>{debugBusy === "manual-flow" ? "Testing..." : "Safe Flow Test"}</button></div>}
      >
        <ProviderGrid providers={debugStatus?.providers || {}} />
        {apiProbe && <ProbeGrid apiProbe={apiProbe} />}
        {manualFlow && <div className="alert alert-success">Lead {manualFlow.leadId} passed {manualFlow.steps.join(" -> ")}. Preview: {manualFlow.previewUrl}</div>}
      </Section>
      <Section title="Live Logs">
        <LogColumns uiLogs={uiLogs} backendLogs={backendLogs} />
      </Section>
    </div>
  );
}

function SettingsPage({ debugStatus, leadCount, setLeadCount, forceRegenerate, setForceRegenerate, forceRefresh, setForceRefresh }) {
  return (
    <div className="page-grid">
      <Section title="Settings">
        <div className="control-grid">
          <label>Default lead count<select className="form-select" value={leadCount} onChange={(event) => setLeadCount(Number(event.target.value))}>{[1, 2, 3, 4, 5].map((count) => <option key={count} value={count}>{count}</option>)}</select></label>
          <label className="checkline"><input type="checkbox" checked={forceRegenerate} onChange={(event) => setForceRegenerate(event.target.checked)} />Force regenerate</label>
          <label className="checkline"><input type="checkbox" checked={forceRefresh} onChange={(event) => setForceRefresh(event.target.checked)} />Force discovery refresh</label>
          <div className="stat-box"><strong>Off</strong><span>Gemini images</span></div>
        </div>
      </Section>
      <Section title="Provider Configuration">
        <ProviderGrid providers={debugStatus?.providers || {}} />
      </Section>
    </div>
  );
}

function ProviderGrid({ providers }) {
  const entries = Object.entries(providers);
  if (!entries.length) return <EmptyState title="No provider status" />;
  return (
    <div className="provider-grid">
      {entries.map(([name, provider]) => (
        <article className={`provider-card ${provider.configured ? "ready" : "missing"}`} key={name}>
          <div className="item-head"><h3>{name}</h3><span className={`badge ${provider.configured ? "text-bg-success" : "text-bg-danger"}`}>{provider.configured ? "READY" : "CHECK"}</span></div>
          {(provider.checks || []).map((check) => <span key={check.name}>{check.name}: {check.maskedValue || (check.configured ? "set" : "missing")}</span>)}
        </article>
      ))}
    </div>
  );
}

function ProbeGrid({ apiProbe }) {
  return (
    <div className="probe-grid">
      {apiProbe.checks.map((check) => (
        <article className="probe-item" key={check.name}>
          <span className={`badge ${statusBadgeClass(check.status)}`}>{check.status}</span>
          <strong>{check.name}</strong>
          <p>{check.message}</p>
        </article>
      ))}
    </div>
  );
}

function LogColumns({ uiLogs, backendLogs }) {
  const fallbackUi = [{ id: "ui-empty", level: "info", event: "idle", message: "No UI actions yet." }];
  const fallbackBackend = [{ id: "backend-empty", level: "INFO", event: "idle", message: "No backend logs loaded." }];
  return (
    <div className="log-columns">
      <LogPane title="UI Actions" logs={uiLogs.length ? uiLogs : fallbackUi} />
      <LogPane title="Backend Background" logs={backendLogs.length ? backendLogs : fallbackBackend} />
    </div>
  );
}

function LogPane({ title, logs }) {
  return (
    <div className="log-pane">
      <h3>{title}</h3>
      {logs.map((log) => (
        <div className="log-row" key={log.id || `${log.event}-${log.timestamp}`}>
          <span className={`badge ${logBadgeClass(log.level)}`}>{log.level}</span>
          <div><strong>{log.event}</strong><p>{log.message}</p></div>
        </div>
      ))}
    </div>
  );
}

function App() {
  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AppShell />
    </BrowserRouter>
  );
}

export default App;
