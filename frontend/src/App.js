import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import axios from "axios";
import { gsap } from "gsap";
import "./App.css";

const API_BASE = (process.env.REACT_APP_API_BASE || "http://127.0.0.1:8000").replace(/\/$/, "");
const MAX_UI_LOGS = 80;

const FALLBACK_PRESETS = [
  { id: "restaurants", label: "Restaurants", industry: "Restaurant", description: "Local restaurants and cafes." },
  { id: "plumbers", label: "Plumbers", industry: "Plumbing", description: "Local plumbing services." },
  { id: "dentists", label: "Dentists", industry: "Dental", description: "Dental practices and oral care providers." },
  { id: "beauty-salons", label: "Beauty Salons", industry: "Beauty", description: "Beauty salons, spas, and care studios." },
  { id: "gyms-fitness", label: "Gyms/Fitness", industry: "Fitness", description: "Gyms, trainers, and wellness studios." },
];

const NAV_ITEMS = [
  { path: "/pipeline", label: "Operations Hub", icon: "▦" },
  { path: "/deployments", label: "Deployments", icon: "↗" },
  { path: "/pipeline-runs", label: "Runs", icon: "⌁" },
  { path: "/admin", label: "Settings", icon: "⚙" },
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

function deploymentModeLabel(item = {}) {
  const mode = item.deploymentMode || item.deployment_mode || item.publishMode || item.publish_mode || item.raw?.deploymentMode || item.raw?.publishMode;
  if (mode === "direct-netlify-fallback") return "Direct Netlify fallback";
  if (String(item.status || item.state || "").toUpperCase().includes("FAILED")) return "Failed";
  if (String(mode).toLowerCase().includes("fallback")) return "Direct Netlify fallback";
  return "GitHub \u2192 Netlify";
}

function deploymentModeBadgeClass(item = {}) {
  const label = deploymentModeLabel(item);
  if (label === "Direct Netlify fallback") return "text-bg-warning";
  if (label === "Failed") return "text-bg-danger";
  return "text-bg-info";
}

function netlifyUrlFromApproval(approval = {}) {
  return approval.deploymentHistory?.url || approval.deployment?.url || approval.raw?.url;
}

function normalizeErrors(errors) {
  if (!Array.isArray(errors)) return [];
  return errors.map((error) => {
    if (typeof error === "string") return error;
    return error?.message || error?.detail || error?.error || JSON.stringify(error);
  });
}

function TechnicalDetails({ data }) {
  if (!data || (Array.isArray(data) && !data.length)) return null;
  if (!Array.isArray(data) && typeof data === "object" && !Object.keys(data).length) return null;
  return (
    <details className="technical-details">
      <summary>View technical details</summary>
      <pre>{JSON.stringify(data, null, 2)}</pre>
    </details>
  );
}

function ErrorSummary({ errors }) {
  const messages = normalizeErrors(errors);
  if (!messages.length) return null;
  return (
    <div className="error-summary">
      {messages.map((message, index) => <p key={`${message}-${index}`}>{message}</p>)}
      <TechnicalDetails data={errors} />
    </div>
  );
}

function Section({ title, help, action, children }) {
  return (
    <section className="panel entry-animate">
      <div className="panel-head">
        <div>
          <h2>{title}</h2>
          {help && <p className="section-help">{help}</p>}
        </div>
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

function LoadingOverlay({ show, message }) {
  if (!show) return null;
  return (
    <div className="loading-overlay" role="status" aria-live="polite">
      <div className="loading-card">
        <div className="loading-spinner" aria-hidden="true" />
        <strong>{message || "Working..."}</strong>
        <span>AI Site Factory is processing this step. Larger batches can take a few minutes.</span>
      </div>
    </div>
  );
}

function AppShell() {
  const locationPath = useLocation();
  const contentRef = useRef(null);

  const [presets, setPresets] = useState(FALLBACK_PRESETS);
  const [selectedPresetId, setSelectedPresetId] = useState("restaurants");
  const [location, setLocation] = useState("Durban, South Africa");
  const [customQuery, setCustomQuery] = useState("");
  const [leadCount, setLeadCount] = useState(5);
  const [forceRefresh, setForceRefresh] = useState(false);
  const [forceRegenerate, setForceRegenerate] = useState(false);
  const [batchId, setBatchId] = useState(null);
  const [leads, setLeads] = useState([]);
  const [selectedLeadKeys, setSelectedLeadKeys] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [provinceStats, setProvinceStats] = useState({});
  const [duplicatesSkipped, setDuplicatesSkipped] = useState(0);
  const [discoveryStats, setDiscoveryStats] = useState(null);
  const [lastDiscoveryCached, setLastDiscoveryCached] = useState(false);
  const [pipelineResult, setPipelineResult] = useState(null);
  const [reportingSummary, setReportingSummary] = useState(null);
  const [approvals, setApprovals] = useState([]);
  const [approvalPreviews, setApprovalPreviews] = useState({});
  const [deployments, setDeployments] = useState([]);
  const [sites, setSites] = useState([]);
  const [siteMeta, setSiteMeta] = useState({ page: 1, pageSize: 8, total: 0, totalPages: 1 });
  const [siteFilters, setSiteFilters] = useState({ q: "", status: "all", contactType: "all", page: 1, pageSize: 8 });
  const [operationGroups, setOperationGroups] = useState([]);
  const [operationMeta, setOperationMeta] = useState({ page: 1, pageSize: 10, total: 0, totalPages: 1 });
  const [operationFilters, setOperationFilters] = useState({ status: "all", channel: "all", page: 1, pageSize: 10 });
  const [expandedGroups, setExpandedGroups] = useState({});
  const [zendeskFields, setZendeskFields] = useState({});
  const [zendeskFieldKeys, setZendeskFieldKeys] = useState([]);
  const [pipelineRuns, setPipelineRuns] = useState([]);
  const [selectedRunDetail, setSelectedRunDetail] = useState(null);
  const [debugStatus, setDebugStatus] = useState(null);
  const [backendLogs, setBackendLogs] = useState([]);
  const [uiLogs, setUiLogs] = useState([]);
  const [apiProbe, setApiProbe] = useState(null);
  const [manualFlow, setManualFlow] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [running, setRunning] = useState(false);
  const [busyPhase, setBusyPhase] = useState("");
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
        const siteParams = new URLSearchParams({
          q: siteFilters.q || "",
          status: siteFilters.status || "all",
          contactType: siteFilters.contactType || "all",
          page: String(siteFilters.page || 1),
          pageSize: String(siteFilters.pageSize || 8),
          noWebsiteOnly: "true",
        });
        const operationParams = new URLSearchParams({
          status: operationFilters.status || "all",
          channel: operationFilters.channel || "all",
          page: String(operationFilters.page || 1),
          pageSize: String(operationFilters.pageSize || 10),
        });
        const [summaryResponse, approvalsResponse, deploymentsResponse, runsResponse, sitesResponse, operationsResponse, zendeskFieldsResponse] = await Promise.all([
          axios.get(`${API_BASE}/api/reporting/summary`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/approvals?status=ALL&limit=80`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/deployments/history?limit=80`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/pipeline/runs?limit=40`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/sites?${siteParams.toString()}`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/operations/groups?${operationParams.toString()}`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/settings/zendesk-fields`, { timeout: 15000 }),
        ]);
        setReportingSummary(summaryResponse.data);
        setApprovals(approvalsResponse.data.approvals || []);
        setDeployments(deploymentsResponse.data.deployments || []);
        setPipelineRuns(runsResponse.data.runs || []);
        setSites(sitesResponse.data.sites || []);
        setOperationGroups(operationsResponse.data.groups || []);
        setOperationMeta({
          page: operationsResponse.data.page || 1,
          pageSize: operationsResponse.data.pageSize || 10,
          total: operationsResponse.data.total || 0,
          totalPages: operationsResponse.data.totalPages || 1,
        });
        setZendeskFields(zendeskFieldsResponse.data.fields || {});
        setZendeskFieldKeys(zendeskFieldsResponse.data.keys || []);
        setSiteMeta({
          page: sitesResponse.data.page || 1,
          pageSize: sitesResponse.data.pageSize || 8,
          total: sitesResponse.data.total || 0,
          totalPages: sitesResponse.data.totalPages || 1,
        });
        if (!silent) addUiLog("success", "operations.refresh", "Pipeline reporting refreshed.", { pendingApprovals: summaryResponse.data.metrics?.pendingApprovals || 0 });
      } catch (error) {
        if (!silent) {
          const failure = formatApiError(error);
          addUiLog("danger", "operations.refresh_failed", `Reporting refresh failed: ${failure.message}`, failure);
        }
      }
    },
    [addUiLog, formatApiError, siteFilters, operationFilters]
  );

  useEffect(() => {
    const loadConfig = async () => {
      try {
        const presetResponse = await callApi("Preset API", "get", "/api/presets", null, { timeout: 15000 });
        setPresets(presetResponse.presets || FALLBACK_PRESETS);
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
    refreshOperations(true);
  }, [refreshOperations]);

  useEffect(() => {
    if (!contentRef.current) return;
    try {
      const targets = contentRef.current.querySelectorAll(".entry-animate");
      if (!targets.length) return;
      gsap.fromTo(targets, { y: 12, opacity: 0 }, { y: 0, opacity: 1, duration: 0.42, ease: "power2.out", stagger: 0.04 });
    } catch (error) {
      addUiLog("warning", "animation.skipped", "Entry animation skipped.", { reason: error.message });
    }
  }, [addUiLog, locationPath.pathname]);

  const selectedPreset = useMemo(() => presets.find((preset) => preset.id === selectedPresetId) || presets[0], [presets, selectedPresetId]);
  const selectedLeads = useMemo(() => leads.filter((lead) => selectedLeadKeys.includes(lead.leadKey)), [leads, selectedLeadKeys]);
  const pendingApprovals = approvals.filter((approval) => ["PENDING", "EXPORT_FAILED", "EXPORTING"].includes(approval.status));

  const toggleLead = (leadKey) => {
    setSelectedLeadKeys((current) => (current.includes(leadKey) ? current.filter((key) => key !== leadKey) : [...current, leadKey]));
  };

  const runPipeline = async (leadsOverride = null, batchOverride = null) => {
    const leadsForRun = Array.isArray(leadsOverride) ? leadsOverride : selectedLeads;
    if (!leadsForRun.length) {
      setNotice("Select one or more leads.", "warning");
      return;
    }
    try {
      setRunning(true);
      setBusyPhase("Generating landing pages, exporting GitHub repos, and creating Zendesk tickets...");
      setPipelineResult(null);
      setNotice("Generating pages and exporting repositories...", "info");
      const data = await callApi(
        "Full Pipeline API",
        "post",
        "/api/pipeline/run",
        {
          sourceBatchId: batchOverride || batchId,
          leads: leadsForRun,
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
      setBusyPhase("");
    }
  };

  const discoverLeads = async () => {
    if (!selectedPreset?.id) {
      setNotice("Select a business type first.", "warning");
      return;
    }
    try {
      setDiscovering(true);
      setBusyPhase("Searching for contactable no-website leads...");
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
      const fetchedLeads = data.leads || [];
      setLeads(fetchedLeads);
      setSelectedLeadKeys(fetchedLeads.map((lead) => lead.leadKey));
      setWarnings(data.warnings || []);
      setProvinceStats(data.provinceStats || {});
      setDuplicatesSkipped(data.duplicatesSkipped || 0);
      setDiscoveryStats(data);
      setLastDiscoveryCached(Boolean(data.cached));
      setNotice(
        `${data.cached ? "Loaded cached" : "Fetched"} ${fetchedLeads.length}/${data.requestedCount || leadCount} eligible leads. ` +
          `Raw ${data.rawFetched || 0}; skipped ${data.websitesSkipped || 0} websites, ${data.noContactSkipped || 0} no-contact, ${data.generatedDuplicatesSkipped || data.duplicatesSkipped || 0} duplicates. ` +
          `${fetchedLeads.length ? "Starting generation now." : ""}`,
        fetchedLeads.length ? "success" : "warning"
      );
      refreshOperations(true);
      if (fetchedLeads.length) {
        setBusyPhase("Generating landing pages, exporting GitHub repos, and creating Zendesk tickets...");
        await runPipeline(fetchedLeads, data.batchId);
      }
    } catch (error) {
      const failure = formatApiError(error);
      setLeads([]);
      setNotice(failure.message || "Lead discovery failed.", "danger");
    } finally {
      setDiscovering(false);
      if (!running) setBusyPhase("");
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
      setBusyPhase("Deploying approved site through GitHub → Netlify...");
      const data = await callApi("Approval Deploy API", "post", `/api/approvals/${approvalId}/approve`, { approvedBy: "Pipeline Operator", notes: "Approved from Pipeline Workspace." }, { timeout: 420000 });
      setNotice(`Approved and deployed ${data.businessName}.`, data.status === "APPROVED" ? "success" : "warning");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Approval deployment failed.", "danger");
      refreshOperations(true);
    } finally {
      setApprovalBusy(null);
      setBusyPhase("");
    }
  };

  const retryExport = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      setBusyPhase("Retrying GitHub export for generated site...");
      const data = await callApi("GitHub Export Retry API", "post", `/api/approvals/${approvalId}/retry-export`, { requestedBy: "Pipeline Operator" }, { timeout: 240000 });
      setNotice(`GitHub export ready for ${data.businessName}.`, "success");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "GitHub export retry failed.", "danger");
    } finally {
      setApprovalBusy(null);
      setBusyPhase("");
    }
  };

  const rejectSite = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      setBusyPhase("Rejecting generated approval...");
      const data = await callApi("Approval Reject API", "post", `/api/approvals/${approvalId}/reject`, { rejectedBy: "Pipeline Operator", reason: "Rejected from Pipeline Workspace." });
      setNotice(`Rejected ${data.businessName}.`, "warning");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Reject failed.", "danger");
    } finally {
      setApprovalBusy(null);
      setBusyPhase("");
    }
  };

  const regenerateSite = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      setBusyPhase("Regenerating landing page and GitHub artifact...");
      const data = await callApi("Approval Regenerate API", "post", `/api/approvals/${approvalId}/regenerate`, { requestedBy: "Pipeline Operator" }, { timeout: 600000 });
      setNotice(`Regenerated ${data.businessName}.`, data.status === "EXPORT_FAILED" ? "danger" : "success");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Regenerate failed.", "danger");
    } finally {
      setApprovalBusy(null);
      setBusyPhase("");
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

  const saveZendeskFields = async () => {
    try {
      const data = await callApi("Zendesk Field Settings API", "put", "/api/settings/zendesk-fields", { fields: zendeskFields }, { timeout: 15000 });
      setZendeskFields(data.fields || {});
      setZendeskFieldKeys(data.keys || zendeskFieldKeys);
      setNotice("Zendesk field mapping saved.", "success");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Zendesk field mapping save failed.", "danger");
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
    selectedPresetId,
    setSelectedPresetId,
    selectedPreset,
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
    discoveryStats,
    lastDiscoveryCached,
    batchId,
    pipelineResult,
    reportingSummary,
    approvals,
    pendingApprovals,
    approvalPreviews,
    deployments,
    sites,
    siteMeta,
    siteFilters,
    setSiteFilters,
    operationGroups,
    operationMeta,
    operationFilters,
    setOperationFilters,
    expandedGroups,
    setExpandedGroups,
    zendeskFields,
    setZendeskFields,
    zendeskFieldKeys,
    saveZendeskFields,
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
    busyPhase,
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
      <LoadingOverlay show={Boolean(busyPhase || discovering || running || approvalBusy)} message={busyPhase || "Working on your batch..."} />
      <aside className="sidebar">
        <div className="brand-block">
          <strong>AI Site Factory</strong>
          <span>GitHub to Netlify</span>
        </div>
        <nav>
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.path} to={item.path} className={({ isActive }) => (isActive ? "active" : "")}>
              <span className="nav-icon" aria-hidden="true">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="content-shell" ref={contentRef}>
        <header className="topbar">
          <div>
            <h1>{NAV_ITEMS.find((item) => item.path === locationPath.pathname)?.label || "Pipeline Workspace"}</h1>
            <span className={`badge ${statusBadgeClass(debugStatus?.status)}`}>{debugStatus?.status || "LOADING"}</span>
          </div>
          {message && <div className={`notice ${messageTone}`}>{message}</div>}
        </header>
        <Routes>
          <Route path="/" element={<Navigate to="/pipeline" replace />} />
          <Route path="/dashboard" element={<Navigate to="/pipeline" replace />} />
          <Route path="/lead-discovery" element={<Navigate to="/pipeline" replace />} />
          <Route path="/generate-approval" element={<Navigate to="/pipeline" replace />} />
          <Route path="/settings" element={<Navigate to="/admin" replace />} />
          <Route path="/pipeline" element={<PipelineWorkspacePage {...shared} />} />
          <Route path="/deployments" element={<DeploymentsPage {...shared} />} />
          <Route path="/pipeline-runs" element={<PipelineRunsPage {...shared} />} />
          <Route path="/admin" element={<AdminPage {...shared} />} />
        </Routes>
      </main>
    </div>
  );
}

function PipelineWorkspacePage(props) {
  const {
    presets,
    selectedPresetId,
    setSelectedPresetId,
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
    setSelectedLeadKeys,
    toggleLead,
    discovering,
    discoverLeads,
    running,
    runPipeline,
    warnings,
    duplicatesSkipped,
    discoveryStats,
    lastDiscoveryCached,
    provinceStats,
    pipelineResult,
    reportingSummary,
    pendingApprovals,
    approvals,
    approvalPreviews,
    approvalBusy,
    previewApproval,
    approveSite,
    retryExport,
    rejectSite,
    regenerateSite,
    refreshOperations,
    sites,
    siteMeta,
    siteFilters,
    setSiteFilters,
    operationGroups,
    operationMeta,
    operationFilters,
    setOperationFilters,
    expandedGroups,
    setExpandedGroups,
  } = props;
  const metrics = reportingSummary?.metrics || {};
  return (
    <div className="page-stack">
      <section className="workspace-hero entry-animate">
        <div>
          <span className="hero-kicker">Lead Pipeline Control Center</span>
          <h2>Operations Hub for batches, Zendesk queues, deployments, and follow-up.</h2>
          <p>Work in grouped batches instead of long flat lists. Expand each run to review email leads, phone leads, Zendesk tickets, deployment state, and failed items.</p>
        </div>
        <MetricRail
          metrics={[
            ["Leads", metrics.leadsDiscovered || leads.length || 0],
            ["Batches", operationMeta.total || pipelineResult?.pipelineId ? operationMeta.total || 1 : 0],
            ["Approvals", pendingApprovals.length],
            ["Sites", sites.length || metrics.noWebsiteSites || 0],
          ]}
        />
      </section>

      <Section
        title="Operations Flow Chart"
        help="Each batch is shown as a pipeline diagram. Expand a group to inspect generated businesses, channel tickets, and statuses."
        action={<button className="btn btn-outline-secondary" type="button" onClick={() => refreshOperations(false)}>Refresh</button>}
      >
        <OperationFilters filters={operationFilters} setFilters={setOperationFilters} />
        <OperationGroupList
          groups={operationGroups}
          meta={operationMeta}
          expandedGroups={expandedGroups}
          setExpandedGroups={setExpandedGroups}
          setOperationFilters={setOperationFilters}
          approvalPreviews={approvalPreviews}
          approvalBusy={approvalBusy}
          previewApproval={previewApproval}
          approveSite={approveSite}
          retryExport={retryExport}
          rejectSite={rejectSite}
          regenerateSite={regenerateSite}
        />
      </Section>

      <Section
        title="Create New Batch"
        help="Type the lead intent you want, choose a location, and the app will fetch eligible no-website leads then auto-generate pages, GitHub artifacts, and Zendesk tickets."
        action={<button className="btn btn-primary" type="button" onClick={discoverLeads} disabled={discovering || running}>{discovering || running ? "Working..." : "Search + Auto Generate"}</button>}
      >
        <div className="preset-grid">
          {presets.map((preset) => (
            <button key={preset.id} className={`preset-tile ${selectedPresetId === preset.id ? "selected" : ""}`} type="button" onClick={() => setSelectedPresetId(preset.id)}>
              <strong>{preset.label}</strong>
              <span>{preset.industry}</span>
              <small>{preset.description}</small>
            </button>
          ))}
        </div>
        <div className="control-grid mt-3">
          <label>Location<input className="form-control" value={location} onChange={(event) => setLocation(event.target.value)} /></label>
          <label>Lead intent<input className="form-control" value={customQuery} onChange={(event) => setCustomQuery(event.target.value)} placeholder="e.g. emergency plumbers, dentists with no website" /></label>
          <label>Lead count<input className="form-control" type="number" min="1" max="200" value={leadCount} onChange={(event) => setLeadCount(Math.max(1, Math.min(200, Number(event.target.value) || 1)))} /></label>
          <label className="checkline"><input type="checkbox" checked={forceRefresh} onChange={(event) => setForceRefresh(event.target.checked)} />Force discovery refresh</label>
        </div>
        <div className="status-strip">
          <span className={`badge ${lastDiscoveryCached ? "text-bg-info" : "text-bg-success"}`}>{lastDiscoveryCached ? "CACHE" : "LIVE"}</span>
          <span>{discoveryStats?.eligibleReturned || leads.length || 0}/{discoveryStats?.requestedCount || leadCount} eligible</span>
          <span>{discoveryStats?.rawFetched || 0} raw fetched</span>
          <span>{discoveryStats?.websitesSkipped || 0} websites skipped</span>
          <span>{discoveryStats?.noContactSkipped || 0} no-contact skipped</span>
          <span>{discoveryStats?.generatedDuplicatesSkipped || duplicatesSkipped} generated duplicates skipped</span>
          <span>{discoveryStats?.emailLeads || 0} email</span>
          <span>{discoveryStats?.phoneLeads || 0} phone</span>
          {Object.entries(provinceStats).map(([key, value]) => <span key={key}>{key}: {value.selected || 0}</span>)}
        </div>
        {warnings.map((warning) => <div className="alert alert-warning" key={warning}>{warning}</div>)}
      </Section>

      <Section
        title="Selected Leads"
        help="Review discovered businesses and tick the leads you want to generate pages for."
        action={
          <div className="button-row">
            <button className="btn btn-outline-secondary" type="button" onClick={() => setSelectedLeadKeys(leads.map((lead) => lead.leadKey))} disabled={!leads.length}>Select All</button>
            <button className="btn btn-outline-secondary" type="button" onClick={() => setSelectedLeadKeys([])} disabled={!selectedLeadKeys.length}>Clear</button>
          </div>
        }
      >
        <div className="selection-summary">
          <div><strong>{selectedLeads.length}</strong><span>selected</span></div>
          <p>Search now auto-generates all eligible leads. Manual selection remains available for fallback reruns.</p>
        </div>
        <LeadTable leads={leads} selectedLeadKeys={selectedLeadKeys} toggleLead={toggleLead} />
      </Section>

      <Section
        title="Generate Landing Pages"
        help="Groq compacts the public lead details, then Gemini creates a freeform landing page with enforced Bootstrap, extra styling libraries, and a color widget."
        action={<button className="btn btn-primary" type="button" onClick={() => runPipeline()} disabled={running || discovering || !selectedLeads.length}>{running ? "Running..." : "Manual Run Pipeline"}</button>}
      >
        <div className="control-grid">
          <label className="checkline"><input type="checkbox" checked={forceRegenerate} onChange={(event) => setForceRegenerate(event.target.checked)} />Force regenerate</label>
          <div className="stat-box"><strong>{selectedLeads.length}</strong><span>leads ready</span></div>
          <div className="stat-box"><strong>Freeform</strong><span>Gemini controls layout</span></div>
        </div>
        {pipelineResult?.results?.length ? (
          <div className="result-grid mt-3">
            {pipelineResult.results.map((result) => <PipelineResultCard key={result.leadKey} result={result} />)}
          </div>
        ) : (
          <EmptyState title="No generated pages yet" text="Select leads above, then run the pipeline." />
        )}
      </Section>

      <Section
        title="Approval & Preview"
        help="Review generated pages, open previews, approve or retry deployment, reject, or regenerate."
      >
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
          <EmptyState title="No approvals" text="Generated pages appear here after a successful GitHub export." />
        )}
      </Section>

      <Section
        title="Site Queue"
        help="Search and filter no-website sites across pending, failed, and deployed records."
        action={<button className="btn btn-outline-secondary" type="button" onClick={() => refreshOperations(false)}>Refresh</button>}
      >
        <SiteQueuePanel sites={sites} siteMeta={siteMeta} siteFilters={siteFilters} setSiteFilters={setSiteFilters} />
      </Section>
    </div>
  );
}

function MetricRail({ metrics }) {
  return (
    <div className="metric-grid hero-metrics">
      {metrics.map(([label, value]) => (
        <div className="metric-card" key={label}><span>{value}</span><label>{label}</label></div>
      ))}
    </div>
  );
}

function OperationFilters({ filters, setFilters }) {
  const update = (patch) => setFilters((current) => ({ ...current, page: 1, ...patch }));
  return (
    <div className="filter-bar operation-filter-bar">
      <label>Status<select className="form-select" value={filters.status} onChange={(event) => update({ status: event.target.value })}>
        <option value="all">All</option>
        <option value="PENDING">Pending</option>
        <option value="APPROVED">Live</option>
        <option value="EXPORT_FAILED">Export failed</option>
        <option value="DEPLOY_FAILED">Deploy failed</option>
      </select></label>
      <label>Channel<select className="form-select" value={filters.channel} onChange={(event) => update({ channel: event.target.value })}>
        <option value="all">All</option>
        <option value="email">Email</option>
        <option value="phone">Phone</option>
        <option value="unknown">Unknown</option>
      </select></label>
    </div>
  );
}

function OperationGroupList({ groups, meta, expandedGroups, setExpandedGroups, setOperationFilters, approvalPreviews, approvalBusy, previewApproval, approveSite, retryExport, rejectSite, regenerateSite }) {
  if (!groups.length) return <EmptyState title="No operation groups" text="Run a batch to see grouped leads and Zendesk work here." />;
  const toggle = (groupId) => setExpandedGroups((current) => ({ ...current, [groupId]: !current[groupId] }));
  const goToPage = (page) => setOperationFilters((current) => ({ ...current, page }));
  return (
    <div className="operation-stack">
      {groups.map((group) => (
        <article className="operation-group" key={group.groupId}>
          <button className="operation-group-head" type="button" onClick={() => toggle(group.groupId)}>
            <span className={`badge ${statusBadgeClass(group.status)}`}>{group.status}</span>
            <div>
              <h3>{group.query || "Pipeline batch"} · {group.location || "No location"}</h3>
              <p>{displayDate(group.createdAt)} · {group.pipelineId}</p>
            </div>
            <span>{expandedGroups[group.groupId] ? "Hide" : "Open"}</span>
          </button>
          <div className="operation-stats">
            {[
              ["Leads", group.leadCount],
              ["Duplicates", group.duplicatesSkipped],
              ["Email", group.emailLeads],
              ["Phone", group.phoneLeads],
              ["Generated", group.generated],
              ["Zendesk pending", group.zendeskPending],
              ["Deploy approved", group.deployApproved],
              ["Live", group.live],
              ["Failed", group.failed],
            ].map(([label, value]) => <div key={label}><strong>{value || 0}</strong><span>{label}</span></div>)}
          </div>
          <OperationFlowChart group={group} />
          {expandedGroups[group.groupId] && (
            <div className="operation-nested">
              {(group.approvals || []).length ? (
                group.approvals.map((approval) => (
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
                ))
              ) : (
                <EmptyState title="No matching approvals in this group" />
              )}
            </div>
          )}
        </article>
      ))}
      <div className="pagination-row">
        <button className="btn btn-outline-secondary btn-sm" type="button" disabled={meta.page <= 1} onClick={() => goToPage(meta.page - 1)}>Previous</button>
        <span>Page {meta.page} of {meta.totalPages} | {meta.total} groups</span>
        <button className="btn btn-outline-secondary btn-sm" type="button" disabled={meta.page >= meta.totalPages} onClick={() => goToPage(meta.page + 1)}>Next</button>
      </div>
    </div>
  );
}

function OperationFlowChart({ group }) {
  const steps = group.chartSteps?.length
    ? group.chartSteps
    : [
        { label: "Fetched", value: group.rawFetched || group.leadCount || 0 },
        { label: "Eligible", value: group.eligible || group.leadCount || 0 },
        { label: "Generated", value: group.generated || 0 },
        { label: "GitHub", value: group.githubExported || 0 },
        { label: "Zendesk", value: group.zendeskTickets || 0 },
        { label: "Pending", value: Math.max(0, (group.generated || 0) - (group.live || 0) - (group.failed || 0)) },
        { label: "Live", value: group.live || 0 },
        { label: "Failed", value: group.failed || 0 },
      ];
  const maxValue = Math.max(1, ...steps.map((step) => Number(step.value) || 0));
  return (
    <div className="operation-flow" aria-label={`Pipeline flow for ${group.pipelineId}`}>
      {steps.map((step, index) => {
        const value = Number(step.value) || 0;
        return (
          <Fragment key={step.label}>
            <div className={`flow-node ${step.label.toLowerCase() === "failed" && value ? "flow-failed" : ""}`}>
              <strong>{value}</strong>
              <span>{step.label}</span>
              <div className="flow-bar"><i style={{ width: `${Math.max(8, Math.round((value / maxValue) * 100))}%` }} /></div>
            </div>
            {index < steps.length - 1 && <span className="flow-arrow" aria-hidden="true">→</span>}
          </Fragment>
        );
      })}
    </div>
  );
}

function SiteQueuePanel({ sites, siteMeta, siteFilters, setSiteFilters }) {
  const updateFilter = (patch) => setSiteFilters((current) => ({ ...current, page: 1, ...patch }));
  const goToPage = (page) => setSiteFilters((current) => ({ ...current, page }));
  return (
    <div className="queue-stack">
      <div className="filter-bar">
        <label>Search<input className="form-control" value={siteFilters.q} onChange={(event) => updateFilter({ q: event.target.value })} placeholder="Business, email, phone, status..." /></label>
        <label>Status<select className="form-select" value={siteFilters.status} onChange={(event) => updateFilter({ status: event.target.value })}>
          <option value="all">All</option>
          <option value="pending">Pending</option>
          <option value="failed">Failed</option>
          <option value="deployed">Live</option>
        </select></label>
        <label>Contact<select className="form-select" value={siteFilters.contactType} onChange={(event) => updateFilter({ contactType: event.target.value })}>
          <option value="all">All</option>
          <option value="email">Email</option>
          <option value="phone">Phone</option>
          <option value="unknown">Unknown</option>
        </select></label>
      </div>
      {sites.length ? (
        <div className="site-list">
          {sites.map((site) => (
            <article className="site-row" key={site.approvalId}>
              <div>
                <h3>{site.businessName}</h3>
                <span>{site.context?.industry || "Local service"} | {site.context?.location || "No location"}</span>
              </div>
              <span className={`badge ${statusBadgeClass(site.status)}`}>{site.status}</span>
              <span className="badge text-bg-info">{site.contactType}</span>
              <div className="site-row-link">{site.liveUrl ? <a href={site.liveUrl} target="_blank" rel="noreferrer">{site.liveUrl}</a> : "No live link yet"}</div>
            </article>
          ))}
        </div>
      ) : (
        <EmptyState title="No matching sites" text="Adjust the search or filters." />
      )}
      <div className="pagination-row">
        <button className="btn btn-outline-secondary btn-sm" type="button" disabled={siteMeta.page <= 1} onClick={() => goToPage(siteMeta.page - 1)}>Previous</button>
        <span>Page {siteMeta.page} of {siteMeta.totalPages} | {siteMeta.total} records</span>
        <button className="btn btn-outline-secondary btn-sm" type="button" disabled={siteMeta.page >= siteMeta.totalPages} onClick={() => goToPage(siteMeta.page + 1)}>Next</button>
      </div>
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

function PipelineResultCard({ result }) {
  return (
    <article className="result-card">
      <div className="item-head">
        <h3>{result.businessName}</h3>
        <span className={`badge ${statusBadgeClass(result.status)}`}>{result.status}</span>
      </div>
      <dl>
        <div><dt>Approval</dt><dd>{result.approvalStatus || "N/A"} {result.pendingApprovalId ? `| ${result.pendingApprovalId}` : ""}</dd></div>
        <div><dt>Generation</dt><dd>{result.siteContent?.stylingLibraries?.join(", ") || "Gemini freeform with enforced libraries"}</dd></div>
        {result.githubExport?.repository && <div><dt>GitHub repo</dt><dd>{result.githubExport.repository}</dd></div>}
        {result.githubExport?.commitSha && <div><dt>Commit</dt><dd>{result.githubExport.commitSha}</dd></div>}
      </dl>
      {result.stepHistory?.length > 0 && <StepList steps={result.stepHistory} />}
      {result.errors?.length > 0 && <div className="alert alert-danger">{result.errors.join(" ")}</div>}
    </article>
  );
}

function ApprovalItem({ approval, previewHtml, busy, onPreview, onApprove, onRetry, onReject, onRegenerate }) {
  const hasSuccessfulGithubExport = Boolean(approval.githubExport?.repoUrl && approval.githubExport?.commitSha);
  const hasEmailContact = Boolean(approval.context?.email);
  const hasPhoneContact = Boolean(approval.context?.phone);
  const isPhoneOnly = hasPhoneContact && !hasEmailContact;
  const needsGithubExport = ["PENDING", "DEPLOY_FAILED", "EXPORT_FAILED"].includes(approval.status) && !hasSuccessfulGithubExport;
  const canApprove = ["PENDING", "DEPLOY_FAILED"].includes(approval.status) && hasSuccessfulGithubExport && !isPhoneOnly;
  return (
    <article className="approval-item">
      <div className="item-head">
        <div><h3>{approval.businessName}</h3><span>{approval.approvalId}</span></div>
        <span className={`badge ${statusBadgeClass(approval.status)}`}>{approval.status}</span>
      </div>
      <dl>
        <div><dt>Contact</dt><dd>{hasEmailContact ? (hasPhoneContact ? "Email + phone lead" : "Email lead") : hasPhoneContact ? "Phone lead" : "No contact yet"}</dd></div>
        <div><dt>Created</dt><dd>{displayDate(approval.createdAt)}</dd></div>
        <div><dt>GitHub artifact</dt><dd>{approval.githubExport?.repoUrl ? <a href={approval.githubExport.repoUrl} target="_blank" rel="noreferrer">{approval.githubExport.repository || approval.githubExport.repoUrl}</a> : "Export required before deploy"}</dd></div>
        <div><dt>Commit</dt><dd>{approval.githubExport?.commitSha || "No commit yet"}</dd></div>
        <div><dt>Zendesk</dt><dd>{approval.zendeskTickets?.length ? approval.zendeskTickets.map((ticket) => <span className="channel-chip" key={ticket.id || `${ticket.channel}-${ticket.ticketId}`}>{ticket.channel}: #{ticket.ticketId || "pending"}</span>) : "No intake ticket yet"}</dd></div>
      </dl>
      {approval.status === "DEPLOY_FAILED" && <p className="text-danger mb-2">Deployment failed. Update the Netlify token if needed, then retry deployment.</p>}
      {needsGithubExport && <p className="text-warning mb-2">GitHub export is required before Netlify can deploy this site. Retry export first.</p>}
      {isPhoneOnly && <p className="text-info mb-2">Phone-only lead: deployment approval is handled from email-capable leads. Use Zendesk phone status for dialer follow-up.</p>}
      <div className="button-row">
        <button className="btn btn-outline-secondary btn-sm" type="button" onClick={onPreview} disabled={busy || !approval.previewAvailable}>{previewHtml ? "Hide Preview" : "Preview"}</button>
        {needsGithubExport && <button className="btn btn-outline-primary btn-sm" type="button" onClick={onRetry} disabled={busy}>Retry Export</button>}
        {!isPhoneOnly && <button className="btn btn-success btn-sm" type="button" onClick={onApprove} disabled={busy || !canApprove}>{busy ? "Working..." : approval.status === "DEPLOY_FAILED" ? "Retry Deploy" : "Approve"}</button>}
        <button className="btn btn-outline-primary btn-sm" type="button" onClick={onRegenerate} disabled={busy}>Regenerate</button>
        <button className="btn btn-outline-danger btn-sm" type="button" onClick={onReject} disabled={busy || !["PENDING", "EXPORT_FAILED", "DEPLOY_FAILED"].includes(approval.status)}>Reject</button>
      </div>
      {previewHtml && (
        <div className="approval-inline-preview">
          <div className="item-head">
            <div><h4>Preview for {approval.businessName}</h4><span>{approval.githubExport?.repository || approval.approvalId}</span></div>
          </div>
          <iframe className="approval-preview" title={`Preview ${approval.businessName}`} srcDoc={previewHtml} />
        </div>
      )}
    </article>
  );
}

function DeploymentsPage({ deployments, approvals }) {
  const deploymentFailures = approvals.filter((approval) => approval.status === "DEPLOY_FAILED");
  return (
    <Section title="Deployment History">
      <DeploymentFailureList approvals={deploymentFailures} />
      <DeploymentList deployments={deployments} detailed />
    </Section>
  );
}

function DeploymentFailureList({ approvals }) {
  if (!approvals.length) return null;
  return (
    <div className="deployment-list mb-3">
      {approvals.map((approval) => (
        <article className="deployment-item" key={`failed-${approval.approvalId}`}>
          <div className="item-head">
            <div><h3>{approval.businessName}</h3><span>{approval.pipelineId}</span></div>
            <span className={`badge ${statusBadgeClass(approval.status)}`}>{approval.status}</span>
          </div>
          <dl>
            <div><dt>Netlify URL</dt><dd>{netlifyUrlFromApproval(approval) || "Not deployed yet"}</dd></div>
            <div><dt>Deployment mode</dt><dd><span className={`badge ${deploymentModeBadgeClass(approval)}`}>{deploymentModeLabel(approval)}</span></dd></div>
            <div><dt>Created</dt><dd>{displayDate(approval.createdAt)}</dd></div>
          </dl>
          <ErrorSummary errors={approval.errors} />
          <TechnicalDetails data={{ errors: approval.errors, githubExport: approval.githubExport }} />
        </article>
      ))}
    </div>
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
            <div><dt>Commit</dt><dd>{deployment.commit_sha || deployment.commitSha || deployment.raw?.commitSha || "N/A"}</dd></div>
            <div><dt>Deployment mode</dt><dd><span className={`badge ${deploymentModeBadgeClass(deployment)}`}>{deploymentModeLabel(deployment)}</span></dd></div>
            {detailed && <div><dt>Approved</dt><dd>{displayDate(deployment.deployed_at || deployment.deployedAt)}</dd></div>}
          </dl>
          <ErrorSummary errors={deployment.raw?.errors} />
          {deployment.raw?.fallbackReason && <div className="alert alert-warning">Git-linked deploy failed, so this site used direct Netlify fallback: {deployment.raw.fallbackReason}</div>}
          <TechnicalDetails data={deployment.raw} />
        </article>
      ))}
    </div>
  );
}

function PipelineRunsPage({ pipelineRuns, selectedRunDetail, loadRunDetail }) {
  const selectedRun = selectedRunDetail?.run;
  return (
    <div className="page-stack">
      <Section title="Pipeline Runs" help="Open a run to see each backend step, including GitHub export and Netlify build activity.">
        <RunList runs={pipelineRuns} onSelect={loadRunDetail} />
      </Section>
      <Section title="Run Detail" help="Step history and related approvals are shown vertically for easier scanning.">
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
    <details>
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

function AdminPage({ debugStatus, backendLogs, uiLogs, apiProbe, debugBusy, runApiProbe, runManualFlow, manualFlow, refreshDiagnostics, leadCount, setLeadCount, forceRegenerate, setForceRegenerate, forceRefresh, setForceRefresh, zendeskFields, setZendeskFields, zendeskFieldKeys, saveZendeskFields }) {
  return (
    <div className="page-stack">
      <Section
        title="API Safety Center"
        help="Run local checks first. External probes may touch provider APIs, so keep them for deliberate validation."
        action={<div className="button-row"><button className="btn btn-outline-secondary" type="button" onClick={() => refreshDiagnostics(false)}>Refresh</button><button className="btn btn-primary" type="button" onClick={() => runApiProbe(false)} disabled={Boolean(debugBusy)}>Local Probe</button><button className="btn btn-outline-primary" type="button" onClick={() => runApiProbe(true)} disabled={Boolean(debugBusy)}>External Probe</button><button className="btn btn-outline-secondary" type="button" onClick={runManualFlow} disabled={Boolean(debugBusy)}>{debugBusy === "manual-flow" ? "Testing..." : "Safe Flow Test"}</button></div>}
      >
        {apiProbe && <ProbeGrid apiProbe={apiProbe} />}
        {manualFlow && <div className="alert alert-success">Lead {manualFlow.leadId} passed {manualFlow.steps.join(" -> ")}. Preview: {manualFlow.previewUrl}</div>}
      </Section>
      <Section title="Provider Diagnostics" help="Secrets remain redacted. Use this panel to spot missing provider configuration before running live work.">
        <ProviderGrid providers={debugStatus?.providers || {}} />
      </Section>
      <Section title="Workspace Settings" help="These controls adjust the current pipeline workflow while keeping existing API contracts stable.">
        <div className="control-grid">
          <label>Max leads per search<input className="form-control" type="number" min="1" max="200" value={leadCount} onChange={(event) => setLeadCount(Math.max(1, Math.min(200, Number(event.target.value) || 1)))} /></label>
          <label className="checkline"><input type="checkbox" checked={forceRegenerate} onChange={(event) => setForceRegenerate(event.target.checked)} />Force regenerate</label>
          <label className="checkline"><input type="checkbox" checked={forceRefresh} onChange={(event) => setForceRefresh(event.target.checked)} />Force discovery refresh</label>
          <div className="stat-box"><strong>Off</strong><span>Gemini images</span></div>
        </div>
      </Section>
      <Section
        title="Zendesk Field Mapping"
        help="Create these custom fields in Zendesk, then paste their numeric field IDs here so tickets and webhooks can exchange structured values."
        action={<button className="btn btn-primary" type="button" onClick={saveZendeskFields}>Save Zendesk Fields</button>}
      >
        <ZendeskFieldSettings fields={zendeskFields} setFields={setZendeskFields} fieldKeys={zendeskFieldKeys} />
      </Section>
      <Section title="Model & API Usage" help="The default flow saves usage by caching leads, reusing generated pages, and keeping Gemini image generation off unless enabled in backend environment.">
        <div className="priority-flow">
          {["Apify lead search", "Gemini prompt/final polish", "Groq draft fallback", "GitHub export", "Netlify Git build"].map((item, index) => (
            <div className="priority-step" key={item}><span>{index + 1}</span><strong>{item}</strong></div>
          ))}
        </div>
        <div className="usage-notes">
          <p>Use cached discovery for repeat searches, keep force regenerate off for normal runs, and approve only pages that are ready to deploy.</p>
          <p>Gemini images are disabled by default; fallback visual assets keep generated pages lightweight and predictable.</p>
        </div>
      </Section>
      <Section title="Live Logs" help="Recent UI actions and backend logs are shown side by side for quick diagnostics.">
        <LogColumns uiLogs={uiLogs} backendLogs={backendLogs} />
      </Section>
    </div>
  );
}

function ZendeskFieldSettings({ fields, setFields, fieldKeys }) {
  const labels = {
    canonicalLeadKey: "Canonical lead key",
    pipelineId: "Pipeline ID",
    approvalId: "Approval ID",
    batchId: "Batch ID",
    contactChannel: "Contact channel",
    leadStatus: "Lead status",
    deployRequested: "Deploy requested",
    emailSendRequested: "Email send requested",
    phoneCallStatus: "Phone call status",
    liveUrl: "Live URL",
    sourceUrl: "Source URL",
  };
  const keys = fieldKeys?.length ? fieldKeys : Object.keys(labels);
  const update = (key, value) => setFields((current) => ({ ...current, [key]: value }));
  return (
    <div className="zendesk-field-grid">
      {keys.map((key) => (
        <label key={key}>
          {labels[key] || key}
          <input className="form-control" value={fields?.[key] || ""} onChange={(event) => update(key, event.target.value)} placeholder="Zendesk custom field ID" />
        </label>
      ))}
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
