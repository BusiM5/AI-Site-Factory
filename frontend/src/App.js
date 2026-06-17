import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";
import { Tooltip } from "bootstrap";
import { gsap } from "gsap";
import "./App.css";

const FALLBACK_PRESETS = [
  {
    id: "restaurants",
    label: "Restaurants",
    industry: "Restaurant",
    description: "Local restaurants, cafes, takeaways, and food venues.",
  },
  {
    id: "plumbers",
    label: "Plumbers",
    industry: "Plumbing",
    description: "Emergency plumbing, repairs, leak detection, and maintenance.",
  },
  {
    id: "dentists",
    label: "Dentists",
    industry: "Dental",
    description: "Dental practices, cosmetic dentistry, and oral care providers.",
  },
  {
    id: "beauty-salons",
    label: "Beauty Salons",
    industry: "Beauty",
    description: "Beauty salons, spas, nail bars, and personal care studios.",
  },
  {
    id: "gyms-fitness",
    label: "Gyms/Fitness",
    industry: "Fitness",
    description: "Gyms, personal trainers, wellness studios, and fitness centers.",
  },
];

const FALLBACK_TEMPLATES = [
  {
    id: "default-service",
    name: "Default Service",
    description: "Clean landing page with hero, four services, about, contact, and footer.",
  },
  {
    id: "bold-local",
    name: "Bold Local",
    description: "High-contrast local-business page with strong calls to action.",
  },
  {
    id: "premium-trust",
    name: "Premium Trust",
    description: "Polished trust-led page for professional service businesses.",
  },
];

const MAX_UI_LOGS = 80;

function App() {
  const API_BASE = "http://127.0.0.1:8000";
  const shellRef = useRef(null);
  const flowRef = useRef(null);

  const [presets, setPresets] = useState(FALLBACK_PRESETS);
  const [templates, setTemplates] = useState(FALLBACK_TEMPLATES);
  const [selectedPresetId, setSelectedPresetId] = useState("restaurants");
  const [selectedTemplateId, setSelectedTemplateId] = useState("default-service");
  const [location, setLocation] = useState("Durban, South Africa");
  const [customQuery, setCustomQuery] = useState("");
  const [ownerName, setOwnerName] = useState("");
  const [ownerEmail, setOwnerEmail] = useState("");
  const [ownerStatus, setOwnerStatus] = useState("unassigned");
  const [ownerFilter, setOwnerFilter] = useState("all");
  const [batchId, setBatchId] = useState(null);
  const [leads, setLeads] = useState([]);
  const [selectedLeadKeys, setSelectedLeadKeys] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [provinceStats, setProvinceStats] = useState({});
  const [duplicatesSkipped, setDuplicatesSkipped] = useState(0);
  const [pipelineResult, setPipelineResult] = useState(null);
  const [reportingSummary, setReportingSummary] = useState(null);
  const [approvals, setApprovals] = useState([]);
  const [approvalPreviews, setApprovalPreviews] = useState({});
  const [deployments, setDeployments] = useState([]);
  const [pipelineRuns, setPipelineRuns] = useState([]);
  const [discovering, setDiscovering] = useState(false);
  const [running, setRunning] = useState(false);
  const [approvalBusy, setApprovalBusy] = useState(null);
  const [ownerBusy, setOwnerBusy] = useState(null);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState("info");
  const [debugStatus, setDebugStatus] = useState(null);
  const [backendLogs, setBackendLogs] = useState([]);
  const [uiLogs, setUiLogs] = useState([]);
  const [apiProbe, setApiProbe] = useState(null);
  const [manualFlow, setManualFlow] = useState(null);
  const [debugBusy, setDebugBusy] = useState(null);
  const [forceRegenerate, setForceRegenerate] = useState(false);
  const [publishMode, setPublishMode] = useState("direct-netlify");

  const addUiLog = useCallback((level, event, text, details = {}) => {
    const entry = {
      id: `${Date.now()}-${Math.random()}`,
      timestamp: new Date().toISOString(),
      level,
      event,
      message: text,
      details,
    };
    setUiLogs((current) => [entry, ...current].slice(0, MAX_UI_LOGS));
  }, []);

  const formatApiError = useCallback((error) => {
    const detail = error.response?.data?.detail;
    return {
      status: error.response?.status || "NETWORK",
      requestId: error.response?.headers?.["x-request-id"],
      message:
        typeof detail === "string"
          ? detail
        : detail?.message || error.message || "Unknown API failure",
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
        const response = await axios({
          method,
          url: `${API_BASE}${path}`,
          data,
          timeout: options.timeout || 180000,
        });
        addUiLog("success", "api.success", `${label} succeeded.`, {
          method,
          path,
          status: response.status,
          requestId: response.headers?.["x-request-id"],
        });
        return response.data;
      } catch (error) {
        const failure = formatApiError(error);
        addUiLog("danger", "api.failure", `${label} failed: ${failure.message}`, {
          method,
          path,
          ...failure,
        });
        throw error;
      }
    },
    [API_BASE, addUiLog, formatApiError]
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
        if (!silent) {
          addUiLog("success", "debug.refresh", "Diagnostics refreshed.", {
            status: statusResponse.data.status,
          });
        }
      } catch (error) {
        const failure = formatApiError(error);
        if (!silent) {
          addUiLog("danger", "debug.refresh_failed", `Diagnostics failed: ${failure.message}`, failure);
        }
      }
    },
    [API_BASE, addUiLog, formatApiError]
  );

  const refreshOperations = useCallback(
    async (silent = false) => {
      try {
        const [summaryResponse, approvalsResponse, deploymentsResponse, runsResponse] = await Promise.all([
          axios.get(`${API_BASE}/api/reporting/summary`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/approvals?status=ALL&limit=50`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/deployments/history?limit=50`, { timeout: 15000 }),
          axios.get(`${API_BASE}/api/pipeline/runs?limit=20`, { timeout: 15000 }),
        ]);
        setReportingSummary(summaryResponse.data);
        setApprovals(approvalsResponse.data.approvals || []);
        setDeployments(deploymentsResponse.data.deployments || []);
        setPipelineRuns(runsResponse.data.runs || []);
        if (!silent) {
          addUiLog("success", "operations.refresh", "Pipeline reporting refreshed.", {
            pendingApprovals: summaryResponse.data.metrics?.pendingApprovals || 0,
          });
        }
      } catch (error) {
        const failure = formatApiError(error);
        if (!silent) {
          addUiLog("danger", "operations.refresh_failed", `Reporting refresh failed: ${failure.message}`, failure);
        }
      }
    },
    [API_BASE, addUiLog, formatApiError]
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
        setNotice("Backend configuration could not be loaded. Fallback presets are active.", "warning");
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
    if (!shellRef.current) return undefined;
    try {
      gsap.fromTo(
        shellRef.current.querySelectorAll(".entry-animate"),
        { y: 14, opacity: 0 },
        { y: 0, opacity: 1, duration: 0.55, ease: "power2.out", stagger: 0.05 }
      );
    } catch (error) {
      addUiLog("warning", "animation.skipped", "Entry animation could not initialize.", { reason: error.message });
    }
    return undefined;
  }, [addUiLog]);

  const selectedPreset = useMemo(
    () => presets.find((preset) => preset.id === selectedPresetId) || presets[0],
    [presets, selectedPresetId]
  );

  const selectedTemplate = useMemo(
    () =>
      templates.find((template) => template.id === selectedTemplateId) ||
      templates[0],
    [templates, selectedTemplateId]
  );

  const selectedLeads = useMemo(
    () => leads.filter((lead) => selectedLeadKeys.includes(lead.leadKey)),
    [leads, selectedLeadKeys]
  );

  const filteredLeads = useMemo(() => {
    if (ownerFilter === "all") return leads;
    if (ownerFilter === "unassigned") {
      return leads.filter((lead) => !lead.ownerName && !lead.ownerEmail);
    }
    return leads.filter((lead) => (lead.ownerEmail || lead.ownerName || "").toLowerCase() === ownerFilter);
  }, [leads, ownerFilter]);

  const ownerFilterOptions = useMemo(() => {
    const owners = new Map();
    leads.forEach((lead) => {
      const value = (lead.ownerEmail || lead.ownerName || "").toLowerCase();
      if (value) {
        owners.set(value, lead.ownerName || lead.ownerEmail);
      }
    });
    return Array.from(owners.entries()).map(([value, label]) => ({ value, label }));
  }, [leads]);

  const pendingApprovalCount =
    pipelineResult?.results?.filter((result) => result.status === "PENDING_APPROVAL").length ||
    approvals.filter((approval) => approval.status === "PENDING").length ||
    0;

  const failedCount =
    pipelineResult?.results?.filter((result) => result.status === "FAILED").length || 0;

  const flowSteps = useMemo(
    () => [
      {
        key: "config",
        label: "Config",
        detail: debugStatus?.status === "READY" ? "Providers configured" : "Needs review",
        state: debugStatus?.status === "READY" ? "complete" : "warning",
      },
      {
        key: "discover",
        label: "Discover",
        detail: discovering ? "Searching maps" : leads.length ? `${leads.length} leads` : "Waiting",
        state: discovering ? "active" : leads.length ? "complete" : "idle",
      },
      {
        key: "select",
        label: "Select",
        detail: selectedLeads.length ? `${selectedLeads.length} queued` : "No leads",
        state: selectedLeads.length ? "complete" : "idle",
      },
      {
        key: "pipeline",
        label: forceRegenerate ? "Regenerate" : "Resume",
        detail: running ? (forceRegenerate ? "Models active" : "Checking saved work") : pipelineResult?.status || "Not run",
        state: running ? "active" : pipelineResult ? (failedCount ? "danger" : "complete") : "idle",
      },
      {
        key: "approval",
        label: "Approval",
        detail: pendingApprovalCount ? `${pendingApprovalCount} pending` : "Waiting",
        state: pendingApprovalCount ? "warning" : approvals.length ? "complete" : "idle",
      },
      {
        key: "github",
        label: "GitHub",
        detail: publishMode === "github-netlify" ? "Optional export on" : "Optional",
        state:
          approvalBusy && publishMode === "github-netlify"
            ? "active"
            : deployments.some((deployment) => deployment.publishMode === "github-netlify" || deployment.raw?.publishMode === "github-netlify")
              ? "complete"
              : "idle",
      },
      {
        key: "deploy",
        label: "Deploy",
        detail: deployments.length ? `${deployments.length} deploys` : "After approval",
        state: deployments.length ? "complete" : approvalBusy ? "active" : "idle",
      },
    ],
    [approvalBusy, approvals.length, debugStatus, deployments, discovering, failedCount, forceRegenerate, leads.length, pendingApprovalCount, pipelineResult, publishMode, running, selectedLeads.length]
  );

  useEffect(() => {
    if (!flowRef.current) return undefined;
    const activeNodes = flowRef.current.querySelectorAll(".flow-step.active");
    const pulse = flowRef.current.querySelector(".flow-energy");

    try {
      gsap.killTweensOf(activeNodes);
      gsap.killTweensOf(pulse);

      if (activeNodes.length && pulse) {
        gsap.to(activeNodes, {
          y: -3,
          duration: 0.7,
          repeat: -1,
          yoyo: true,
          ease: "sine.inOut",
        });
        gsap.fromTo(
          pulse,
          { xPercent: -8, opacity: 0.25 },
          { xPercent: 108, opacity: 1, duration: 1.45, repeat: -1, ease: "power1.inOut" }
        );
      }
    } catch (error) {
      addUiLog("warning", "animation.skipped", "Flow animation could not initialize.", { reason: error.message });
    }

    return () => {
      try {
        gsap.killTweensOf(activeNodes);
        gsap.killTweensOf(pulse);
      } catch (error) {
        // Animation cleanup should never block app teardown.
      }
    };
  }, [addUiLog, flowSteps]);

useEffect(() => {
  const tooltipElements = Array.from(
    document.querySelectorAll('[data-bs-toggle="tooltip"]')
  );

  const tooltips = tooltipElements.map((element) => {
    const existingTooltip = Tooltip.getInstance(element);
    if (existingTooltip) {
      existingTooltip.dispose();
    }

    return new Tooltip(element, {
      trigger: "hover",
      boundary: "window",
    });
  });

  return () => {
    tooltips.forEach((tooltip) => {
      try {
        tooltip.dispose();
      } catch (error) {
        // Ignore tooltip cleanup errors
      }
    });
  };
}, []);

  const toggleLead = (leadKey) => {
    setSelectedLeadKeys((current) =>
      current.includes(leadKey)
        ? current.filter((key) => key !== leadKey)
        : [...current, leadKey]
    );
  };

  const selectAllLeads = () => {
    setSelectedLeadKeys(filteredLeads.map((lead) => lead.leadKey));
    addUiLog("info", "leads.select_all", "Visible leads selected.", { count: filteredLeads.length });
  };

  const clearSelectedLeads = () => {
    setSelectedLeadKeys([]);
    addUiLog("info", "leads.clear", "Lead selection cleared.");
  };

  const discoverLeads = async () => {
    if (!selectedPreset?.id) {
      setNotice("Select a business type first.", "warning");
      return;
    }

    try {
      setDiscovering(true);
      setPipelineResult(null);
      setWarnings([]);
      setProvinceStats({});
      setDuplicatesSkipped(0);
      setSelectedLeadKeys([]);
      setNotice("Searching Google Maps with Apify...", "info");

      const data = await callApi(
        "Lead Discovery API",
        "post",
        "/api/leads/discover",
        {
          presetId: selectedPreset.id,
          location: location || "Durban, South Africa",
          query: customQuery || null,
          limit: 3,
          ownerName: ownerName || null,
          ownerEmail: ownerEmail || null,
          ownerStatus,
        },
        { timeout: 600000 }
      );

      setBatchId(data.batchId);
      setLeads(data.leads || []);
      setWarnings(data.warnings || []);
      setProvinceStats(data.provinceStats || {});
      setDuplicatesSkipped(data.duplicatesSkipped || 0);

      setNotice(
        `Fetched ${data.leads?.length || 0} new South African leads. Skipped ${data.duplicatesSkipped || 0} duplicates.`,
        data.leads?.length ? "success" : "warning"
      );

      refreshDiagnostics(true);
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setLeads([]);
      setBatchId(null);
      setProvinceStats({});
      setDuplicatesSkipped(0);
      setNotice(failure.message || "Lead discovery failed. Check backend provider settings.", "danger");
      refreshDiagnostics(true);
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
      setNotice(
        forceRegenerate
          ? "Forcing fresh HTML generation and preparing new approvals."
          : "Resuming saved work first. New HTML is generated only for leads without reusable output.",
        "info"
      );

      const data = await callApi(
        "Full Pipeline API",
        "post",
        "/api/pipeline/run",
        {
          sourceBatchId: batchId,
          templateId: selectedTemplate.id,
          leads: selectedLeads,
          regenerateExistingSites: true,
          resumeExisting: true,
          forceRegenerate,
        },
        { timeout: 600000 }
      );

      setPipelineResult(data);
      setNotice(
        `Pipeline finished with status ${data.status}. Reused/skipped steps are shown in each result history.`,
        data.status === "PENDING_APPROVAL" || data.status === "PARTIAL_PENDING" ? "warning" : data.status === "COMPLETED" ? "success" : "warning"
      );
      refreshDiagnostics(true);
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Pipeline run failed. Check debug logs for the failed provider.", "danger");
      refreshDiagnostics(true);
    } finally {
      setRunning(false);
    }
  };

  const approveSite = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      setNotice(
        publishMode === "github-netlify"
          ? "Approving site, exporting HTML to GitHub, then deploying to Netlify..."
          : "Approving site and deploying to Netlify...",
        "info"
      );
      const data = await callApi(
        "Approval Deploy API",
        "post",
        `/api/approvals/${approvalId}/approve`,
        {
          approvedBy: ownerName || "Dashboard Operator",
          notes: "Approved from dashboard.",
          regenerateExistingSite: true,
          publishMode,
        },
        { timeout: 420000 }
      );
      setNotice(
        data.zendesk?.ticketUrl
          ? `Approved and deployed ${data.businessName}${data.githubExport ? " via GitHub" : ""}. Zendesk ticket created.`
          : `Approved and deployed ${data.businessName}${data.githubExport ? " via GitHub" : ""}. Zendesk needs review.`,
        data.status === "APPROVED" ? "success" : "warning"
      );
      refreshDiagnostics(true);
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Approval deployment failed.", "danger");
      refreshDiagnostics(true);
      refreshOperations(true);
    } finally {
      setApprovalBusy(null);
    }
  };

  const rejectSite = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      const data = await callApi(
        "Approval Reject API",
        "post",
        `/api/approvals/${approvalId}/reject`,
        {
          rejectedBy: ownerName || "Dashboard Operator",
          reason: "Rejected from dashboard.",
        },
        { timeout: 60000 }
      );
      setNotice(`Rejected ${data.businessName}.`, "warning");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Approval rejection failed.", "danger");
    } finally {
      setApprovalBusy(null);
    }
  };

  const regenerateSite = async (approvalId) => {
    try {
      setApprovalBusy(approvalId);
      setNotice("Regenerating HTML for manual approval...", "info");
      const data = await callApi(
        "Approval Regenerate API",
        "post",
        `/api/approvals/${approvalId}/regenerate`,
        {
          requestedBy: ownerName || "Dashboard Operator",
          notes: "Regenerated from dashboard.",
        },
        { timeout: 420000 }
      );
      setNotice(`Regenerated ${data.businessName}. New approval is pending.`, "warning");
      refreshDiagnostics(true);
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Regeneration failed.", "danger");
      refreshDiagnostics(true);
    } finally {
      setApprovalBusy(null);
    }
  };

  const previewApproval = async (approvalId) => {
    if (approvalPreviews[approvalId]) {
      setApprovalPreviews((current) => {
        const next = { ...current };
        delete next[approvalId];
        return next;
      });
      return;
    }

    try {
      setApprovalBusy(approvalId);
      const data = await callApi(
        "Approval Preview API",
        "get",
        `/api/approvals/${approvalId}?includeHtml=true`,
        null,
        { timeout: 60000 }
      );
      setApprovalPreviews((current) => ({
        ...current,
        [approvalId]: data.pendingPreviewHtml || "",
      }));
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Could not load approval preview.", "danger");
    } finally {
      setApprovalBusy(null);
    }
  };

  const updateLeadOwner = async (lead) => {
    const canonicalKey = lead.canonicalLeadKey || lead.leadKey;
    try {
      setOwnerBusy(canonicalKey);
      const data = await callApi(
        "Lead Owner API",
        "post",
        `/api/leads/${canonicalKey}/owner`,
        {
          ownerName: ownerName || null,
          ownerEmail: ownerEmail || null,
          ownerStatus,
        },
        { timeout: 60000 }
      );
      setLeads((current) =>
        current.map((item) =>
          (item.canonicalLeadKey || item.leadKey) === canonicalKey
            ? {
                ...item,
                ownerName: data.ownerName,
                ownerEmail: data.ownerEmail,
                ownerStatus: data.ownerStatus,
                assignedAt: data.assignedAt,
              }
            : item
        )
      );
      setNotice(`Owner updated for ${data.businessName}.`, "success");
      refreshOperations(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Owner update failed.", "danger");
    } finally {
      setOwnerBusy(null);
    }
  };

  const runProbe = async (includeExternal) => {
    try {
      setDebugBusy(includeExternal ? "external-probe" : "local-probe");
      setNotice(includeExternal ? "Validating provider APIs..." : "Checking local backend diagnostics...", "info");
      const data = await callApi(
        includeExternal ? "Provider Validation API" : "Local Probe API",
        "post",
        "/api/debug/probe",
        { includeExternal },
        { timeout: includeExternal ? 180000 : 30000 }
      );
      setApiProbe(data);
      setNotice(
        data.status === "VALID" ? "API validation passed." : "API validation found failures.",
        data.status === "VALID" ? "success" : "danger"
      );
      refreshDiagnostics(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "API validation failed.", "danger");
    } finally {
      setDebugBusy(null);
    }
  };

  const runManualFlowTest = async () => {
    try {
      setDebugBusy("manual-flow");
      setManualFlow(null);
      setNotice("Running safe local API flow test...", "info");

      const scrape = await callApi("Scrape API", "post", "/api/scrape/lead", {
        url: "https://example.com",
      });
      const leadPayload = {
        businessName: scrape.businessName || "Example Business",
        email: scrape.email || "info@example.com",
        domain: scrape.domain || "example.com",
        category: scrape.category || "General Services",
        location: scrape.location || "South Africa",
        notes: scrape.notes || "Debugger generated sample lead.",
      };
      const intake = await callApi("Lead Intake API", "post", "/api/leads/intake", {
        rawLeadRow: leadPayload,
        sourceType: "ui-debugger",
        batchId: `debug-${Date.now()}`,
      });
      const clean = await callApi("Lead Clean API", "post", `/api/leads/${intake.leadId}/clean`);
      const content = await callApi("Content Generation API", "post", "/api/content/generate", {
        leadRecord: clean,
        generationProfile: "debug",
        templateId: selectedTemplate.id,
        toneProfile: "professional",
      });
      const preview = await callApi("Preview Build API", "post", "/api/site/build-preview", {
        leadId: clean.leadId,
        contentPacket: content.contentPacket,
        templateId: selectedTemplate.id,
        deployMode: "preview",
      });
      const leadRecord = await callApi("Lead Lookup API", "get", `/api/leads/${intake.leadId}`);
      const outreach = await callApi("Outreach Draft API", "post", "/api/outreach/generate", {
        leadId: clean.leadId,
        businessName: clean.businessName,
        email: clean.email,
        category: clean.category,
        previewReference: preview.previewUrl,
      });

      const result = {
        status: "VALID",
        leadId: intake.leadId,
        steps: [
          "scrape",
          "intake",
          "clean",
          "content",
          "preview",
          "lookup",
          "outreach",
        ],
        previewUrl: preview.previewUrl,
        outreachSubject: outreach.subject,
        storedLeadStatus: leadRecord.intakeStatus || "LOADED",
      };
      setManualFlow(result);
      setNotice("Safe local API flow test passed.", "success");
      refreshDiagnostics(true);
    } catch (error) {
      const failure = formatApiError(error);
      const result = { status: "INVALID", message: failure.message, failure };
      setManualFlow(result);
      setNotice(`Safe local API flow failed: ${failure.message}`, "danger");
    } finally {
      setDebugBusy(null);
    }
  };

  const statusBadgeClass = (status) => {
    if (status?.startsWith("COMPLETED") || status === "VALID" || status === "READY") return "text-bg-success";
    if (status === "PROCESSING" || status === "ACTION_REQUIRED" || status === "PENDING_APPROVAL" || status === "PARTIAL_PENDING" || status === "SKIPPED") return "text-bg-warning";
    if (status === "FAILED" || status === "INVALID" || status === "PARTIAL_FAILURE" || status === "DEPLOYED_ZENDESK_FAILED" || status === "DEPLOY_FAILED" || status === "PUBLISH_FAILED") return "text-bg-danger";
    return "text-bg-secondary";
  };

  const logBadgeClass = (level) => {
    if (level === "success" || level === "INFO") return "text-bg-success";
    if (level === "warning" || level === "WARNING") return "text-bg-warning";
    if (level === "danger" || level === "ERROR") return "text-bg-danger";
    return "text-bg-secondary";
  };

  return (
    <div className="app-shell" ref={shellRef}>
      <header className="app-hero entry-animate">
        <div className="container-fluid py-4">
          <div className="d-flex flex-column flex-xl-row gap-4 align-items-xl-center justify-content-between">
            <div>
              <span className="eyebrow">AI Site Factory</span>
              <h1 className="display-title">Lead Pipeline Control Center</h1>
              <p className="hero-copy">
                Discover leads, generate sites, deploy to Netlify, sync Zendesk, and watch every API step with safe diagnostics.
              </p>
            </div>
            <div className="metric-strip">
              <div className="metric-card">
                <span>{leads.length}</span>
                <label>Leads</label>
              </div>
              <div className="metric-card">
                <span>{selectedLeads.length}</span>
                <label>Queued</label>
              </div>
              <div className="metric-card">
                <span>{pendingApprovalCount}</span>
                <label>Pending</label>
              </div>
              <div className="metric-card">
                <span>{deployments.length || reportingSummary?.metrics?.approvedDeployments || 0}</span>
                <label>Deploys</label>
              </div>
            </div>
          </div>
        </div>
      </header>

      <main className="container-fluid py-4">
        {message && (
          <div className={`alert alert-${messageTone} border-0 shadow-sm entry-animate`} role="alert">
            {message}
          </div>
        )}

        <section className="flow-card entry-animate" ref={flowRef} aria-label="Active automation flow">
          <div className="flow-track">
            <span className="flow-energy" />
            {flowSteps.map((step) => (
              <div className={`flow-step ${step.state}`} key={step.key}>
                <span className="flow-dot" />
                <strong>{step.label}</strong>
                <small>{step.detail}</small>
              </div>
            ))}
          </div>
        </section>

        <div className="row g-4">
          <section className="col-12 col-xl-5">
            <div className="card control-card h-100 entry-animate">
              <div className="card-header bg-white border-0 pb-0">
                <div className="d-flex align-items-start justify-content-between gap-3">
                  <div>
                    <h2 className="h5 mb-1">Lead Discovery</h2>
                    <p className="text-secondary mb-0">{selectedPreset?.description}</p>
                  </div>
                  <button
                    className="btn btn-primary"
                    onClick={discoverLeads}
                    disabled={discovering}
                    data-bs-toggle="tooltip"
                    title="Calls /api/leads/discover, normalizes leads, and records backend logs."
                  >
                    {discovering ? "Searching..." : "Search Leads"}
                  </button>
                </div>
              </div>
              <div className="card-body">
                <div className="preset-grid">
                  {presets.map((preset) => (
                    <button
                      type="button"
                      key={preset.id}
                      className={`preset-tile ${preset.id === selectedPresetId ? "selected" : ""}`}
                      onClick={() => setSelectedPresetId(preset.id)}
                      data-bs-toggle="tooltip"
                      title={preset.description}
                    >
                      <strong>{preset.label}</strong>
                      <span>{preset.industry}</span>
                    </button>
                  ))}
                </div>

                <div className="row g-3 mt-1">
                  <div className="col-md-6">
                    <label className="form-label">Discovery region</label>
                    <input
                      className="form-control"
                      value={location}
                       onChange={(event) => setLocation(event.target.value)}
                       placeholder="Durban, South Africa"
                    />
                  </div>
                  <div className="col-md-6">
                    <label className="form-label">Search text</label>
                    <input
                      className="form-control"
                      value={customQuery}
                      onChange={(event) => setCustomQuery(event.target.value)}
                      placeholder={selectedPreset?.label || "Business type"}
                      data-bs-toggle="tooltip"
                      title="Optional override. Leave empty to use the selected preset query."
                    />
                  </div>
                  <div className="col-12">
                    <label className="form-label">Site template</label>
                    <select
                      className="form-select"
                      value={selectedTemplateId}
                      onChange={(event) => setSelectedTemplateId(event.target.value)}
                    >
                      {templates.map((template) => (
                        <option key={template.id} value={template.id}>
                          {template.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="col-md-4">
                    <label className="form-label">Owner name</label>
                    <input
                      className="form-control"
                      value={ownerName}
                      onChange={(event) => setOwnerName(event.target.value)}
                      placeholder="Optional owner"
                    />
                  </div>
                  <div className="col-md-4">
                    <label className="form-label">Owner email</label>
                    <input
                      className="form-control"
                      value={ownerEmail}
                      onChange={(event) => setOwnerEmail(event.target.value)}
                      placeholder="owner@example.com"
                    />
                  </div>
                  <div className="col-md-4">
                    <label className="form-label">Owner status</label>
                    <select
                      className="form-select"
                      value={ownerStatus}
                      onChange={(event) => setOwnerStatus(event.target.value)}
                    >
                      <option value="unassigned">Unassigned</option>
                      <option value="assigned">Assigned</option>
                      <option value="working">Working</option>
                      <option value="ready-for-review">Ready for review</option>
                    </select>
                  </div>
                </div>

                <div className="template-callout mt-3">
                  <strong>{selectedTemplate?.name}</strong>
                  <span>{selectedTemplate?.description}</span>
                </div>

                <div className="template-callout mt-3">
                  <strong>All provinces active</strong>
                  <span>
                    Eastern Cape, Free State, Gauteng, KwaZulu-Natal, Limpopo, Mpumalanga, Northern Cape, North West, and Western Cape.
                  </span>
                </div>

                {(Object.keys(provinceStats).length > 0 || duplicatesSkipped > 0) && (
                  <div className="province-grid mt-3">
                    {Object.entries(provinceStats).map(([province, stats]) => (
                      <div className="province-card" key={province}>
                        <strong>{province}</strong>
                        <span>{stats.selected || 0} selected</span>
                        <small>{stats.duplicatesSkipped || 0} duplicates skipped</small>
                      </div>
                    ))}
                    <div className="province-card total">
                      <strong>Duplicates</strong>
                      <span>{duplicatesSkipped}</span>
                      <small>Skipped across stored history</small>
                    </div>
                  </div>
                )}

                {warnings.length > 0 && (
                  <div className="alert alert-warning mt-3 mb-0">
                    {warnings.map((warning) => (
                      <div key={warning}>{warning}</div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </section>

          <section className="col-12 col-xl-7">
            <div className="card control-card h-100 entry-animate">
              <div className="card-header bg-white border-0 pb-0">
                <div className="d-flex align-items-start justify-content-between gap-3">
                  <div>
                    <h2 className="h5 mb-1">API Safety Center</h2>
                    <p className="text-secondary mb-0">
                      Validate configuration, run safe endpoint tests, and review exact failure reasons.
                    </p>
                  </div>
                  <span className={`badge rounded-pill ${statusBadgeClass(debugStatus?.status)}`}>
                    {debugStatus?.status || "UNKNOWN"}
                  </span>
                </div>
              </div>
              <div className="card-body">
                <div className="d-flex flex-wrap gap-2 mb-3">
                  <button className="btn btn-outline-secondary" onClick={() => refreshDiagnostics(false)}>
                    Refresh Diagnostics
                  </button>
                  <button className="btn btn-outline-secondary" onClick={() => refreshOperations(false)}>
                    Refresh Reporting
                  </button>
                  <button
                    className="btn btn-outline-primary"
                    onClick={() => runProbe(false)}
                    disabled={debugBusy === "local-probe"}
                    data-bs-toggle="tooltip"
                    title="Checks backend health and required env config without calling external providers."
                  >
                    {debugBusy === "local-probe" ? "Checking..." : "Local API Probe"}
                  </button>
                  <button
                    className="btn btn-outline-success"
                    onClick={runManualFlowTest}
                    disabled={debugBusy === "manual-flow"}
                    data-bs-toggle="tooltip"
                    title="Runs scrape, intake, clean, content, preview, lookup, and outreach draft APIs with safe sample data."
                  >
                    {debugBusy === "manual-flow" ? "Testing..." : "Safe Flow Test"}
                  </button>
                  <button
                    className="btn btn-outline-danger"
                    onClick={() => runProbe(true)}
                    disabled={debugBusy === "external-probe"}
                    data-bs-toggle="tooltip"
                    title="Calls provider auth/status APIs for Apify, Gemini, Groq, Netlify, and Zendesk. This does not create sites or tickets."
                  >
                    {debugBusy === "external-probe" ? "Validating..." : "Validate Providers"}
                  </button>
                </div>

                <div className="row g-3">
                  {Object.entries(debugStatus?.providers || {}).map(([provider, providerStatus]) => (
                    <div className="col-md-6 col-xxl-4" key={provider}>
                      <div className={`provider-card ${providerStatus.configured ? "ready" : "missing"}`}>
                        <div className="d-flex align-items-center justify-content-between">
                          <strong className="text-capitalize">{provider}</strong>
                          <span className={`badge ${providerStatus.configured ? "text-bg-success" : "text-bg-danger"}`}>
                            {providerStatus.configured ? "Ready" : "Needs key"}
                          </span>
                        </div>
                        <small>
                          {providerStatus.checks
                            .map((check) => `${check.name}: ${check.configured ? "set" : check.issue}`)
                            .join(" | ")}
                        </small>
                      </div>
                    </div>
                  ))}
                </div>

                {reportingSummary?.metrics && (
                  <div className="report-grid mt-3">
                    <div className="report-card">
                      <span>{reportingSummary.metrics.leadsDiscovered || 0}</span>
                      <strong>Stored Leads</strong>
                    </div>
                    <div className="report-card">
                      <span>{reportingSummary.metrics.duplicatesSkipped || 0}</span>
                      <strong>Duplicates Skipped</strong>
                    </div>
                    <div className="report-card">
                      <span>{reportingSummary.metrics.pendingApprovals || 0}</span>
                      <strong>Pending Approvals</strong>
                    </div>
                    <div className="report-card">
                      <span>{reportingSummary.metrics.approvedDeployments || 0}</span>
                      <strong>Deployments</strong>
                    </div>
                    <div className="report-card">
                      <span>{reportingSummary.metrics.zendeskTickets || 0}</span>
                      <strong>Zendesk Tickets</strong>
                    </div>
                    <div className="report-card">
                      <span>{reportingSummary.metrics.failedSteps || 0}</span>
                      <strong>Failed Steps</strong>
                    </div>
                    <div className="report-card">
                      <span>{reportingSummary.metrics.pipelineRuns || 0}</span>
                      <strong>Pipeline Runs</strong>
                    </div>
                    <div className="report-card">
                      <span>{reportingSummary.metrics.activePipelineRuns || 0}</span>
                      <strong>Active Runs</strong>
                    </div>
                  </div>
                )}

                {(apiProbe || manualFlow) && (
                  <div className="debug-output mt-3">
                    {apiProbe && (
                      <div>
                        <div className="d-flex align-items-center justify-content-between mb-2">
                          <strong>Probe Result</strong>
                          <span className={`badge ${statusBadgeClass(apiProbe.status)}`}>{apiProbe.status}</span>
                        </div>
                        <div className="probe-grid">
                          {apiProbe.checks.map((check) => (
                            <div className="probe-item" key={check.name}>
                              <span className={`badge ${statusBadgeClass(check.status)}`}>{check.status}</span>
                              <strong>{check.name}</strong>
                              <small>{check.message}</small>
                              <small>{check.durationMs} ms</small>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {manualFlow && (
                      <div className="mt-3">
                        <div className="d-flex align-items-center justify-content-between mb-2">
                          <strong>Safe Flow Result</strong>
                          <span className={`badge ${statusBadgeClass(manualFlow.status)}`}>{manualFlow.status}</span>
                        </div>
                        {manualFlow.status === "VALID" ? (
                          <p className="mb-0">
                            Lead {manualFlow.leadId} passed {manualFlow.steps.join(" -> ")}. Preview: {manualFlow.previewUrl}
                          </p>
                        ) : (
                          <p className="mb-0 text-danger">{manualFlow.message}</p>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </section>
        </div>

        <section className="card control-card mt-4 entry-animate">
          <div className="card-header bg-white border-0 pb-0">
            <div className="d-flex flex-column flex-lg-row align-items-lg-start justify-content-between gap-3">
              <div>
                <h2 className="h5 mb-1">Leads</h2>
                <p className="text-secondary mb-0">
                  {batchId ? `Batch ${batchId}` : "No batch loaded"} {duplicatesSkipped ? `| ${duplicatesSkipped} duplicates skipped` : ""}
                </p>
              </div>
              <div className="d-flex flex-wrap gap-2">
                <select
                  className="form-select owner-filter"
                  value={ownerFilter}
                  onChange={(event) => setOwnerFilter(event.target.value)}
                  aria-label="Filter leads by owner"
                >
                  <option value="all">All owners</option>
                  <option value="unassigned">Unassigned</option>
                  {ownerFilterOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <button className="btn btn-outline-secondary" type="button" onClick={selectAllLeads} disabled={!leads.length}>
                  Select All
                </button>
                <button className="btn btn-outline-secondary" type="button" onClick={clearSelectedLeads} disabled={!selectedLeadKeys.length}>
                  Clear
                </button>
                <label className="form-check d-flex align-items-center gap-2 mb-0">
                  <input
                    className="form-check-input mt-0"
                    type="checkbox"
                    checked={forceRegenerate}
                    onChange={(event) => setForceRegenerate(event.target.checked)}
                  />
                  <span className="small">Force regenerate</span>
                </label>
                <button
                  className="btn btn-primary"
                  onClick={runPipeline}
                  disabled={running || !selectedLeads.length}
                  data-bs-toggle="tooltip"
                  title="Resumes saved work first. Force regenerate creates fresh Gemini and Groq output."
                >
                  {running ? "Running..." : forceRegenerate ? "Regenerate Selected" : "Resume Pipeline"}
                </button>
              </div>
            </div>
          </div>
          <div className="card-body">
            {filteredLeads.length > 0 ? (
              <div className="table-responsive data-table-wrap">
                <table className="table align-middle mb-0">
                  <thead>
                    <tr>
                      <th>Select</th>
                      <th>Business</th>
                      <th>Contact</th>
                      <th>Province</th>
                      <th>Owner</th>
                      <th>Category</th>
                      <th>Rating</th>
                      <th>Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredLeads.map((lead) => (
                      <tr key={lead.leadKey}>
                        <td>
                          <input
                            aria-label={`Select ${lead.businessName}`}
                            className="form-check-input"
                            type="checkbox"
                            checked={selectedLeadKeys.includes(lead.leadKey)}
                            onChange={() => toggleLead(lead.leadKey)}
                          />
                        </td>
                        <td>
                          <strong>{lead.businessName}</strong>
                          <span className="d-block text-secondary small">{lead.address || lead.location}</span>
                        </td>
                        <td>
                          <span>{lead.email || "No email yet"}</span>
                          <span className="d-block text-secondary small">{lead.phone || lead.domain || "No phone yet"}</span>
                        </td>
                        <td>{lead.province || "South Africa"}</td>
                        <td>
                          <span>{lead.ownerName || "Unassigned"}</span>
                          <span className="d-block text-secondary small">{lead.ownerStatus || lead.ownerEmail || "No owner status"}</span>
                          <button
                            className="btn btn-link btn-sm p-0"
                            type="button"
                            onClick={() => updateLeadOwner(lead)}
                            disabled={ownerBusy === (lead.canonicalLeadKey || lead.leadKey)}
                          >
                            {ownerBusy === (lead.canonicalLeadKey || lead.leadKey) ? "Saving..." : "Assign"}
                          </button>
                        </td>
                        <td>{lead.category}</td>
                        <td>{lead.rating ? `${lead.rating} (${lead.reviewsCount || 0})` : "N/A"}</td>
                        <td>
                          {lead.sourceUrl ? (
                            <a href={lead.sourceUrl} target="_blank" rel="noreferrer">
                              Open
                            </a>
                          ) : (
                            "Apify"
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state">
                <h3>No leads loaded</h3>
                <p>Choose a preset and run a search.</p>
              </div>
            )}
          </div>
        </section>

        <div className="row g-4 mt-1">
          <section className="col-12 col-xl-7">
            <div className="card control-card h-100 entry-animate">
              <div className="card-header bg-white border-0 pb-0">
                <div className="d-flex align-items-start justify-content-between gap-3">
                  <div>
                    <h2 className="h5 mb-1">Pipeline Results</h2>
                    <p className="text-secondary mb-0">{pipelineResult?.pipelineId || "No run completed"}</p>
                  </div>
                  {pipelineResult && (
                    <span className={`badge rounded-pill ${statusBadgeClass(pipelineResult.status)}`}>
                      {pipelineResult.status}
                    </span>
                  )}
                </div>
              </div>
              <div className="card-body">
                {running && (
                  <div className="result-grid">
                    {selectedLeads.map((lead) => (
                      <article className="result-card processing" key={lead.leadKey}>
                        <span className="badge text-bg-primary">PROCESSING</span>
                        <h3>{lead.businessName}</h3>
                        <p>{forceRegenerate ? "Queued for fresh Gemini/Groq generation." : "Checking saved approvals and deployments before generating."}</p>
                      </article>
                    ))}
                  </div>
                )}

                {pipelineResult?.results?.length > 0 ? (
                  <div className="result-grid">
                    {pipelineResult.results.map((result) => (
                      <article className="result-card" key={result.leadKey}>
                        <div className="d-flex align-items-start justify-content-between gap-2">
                          <h3>{result.businessName}</h3>
                          <span className={`badge ${statusBadgeClass(result.status)}`}>{result.status}</span>
                        </div>

                        <dl>
                          <div>
                            <dt>Approval</dt>
                            <dd>
                              {result.pendingApprovalId ? (
                                <span>{result.approvalStatus || "PENDING"} | {result.pendingApprovalId}</span>
                              ) : (
                                result.approvalStatus || "No approval"
                              )}
                            </dd>
                          </div>
                          <div>
                            <dt>Current step</dt>
                            <dd>{result.currentStep || result.pipelineStatus || result.status}</dd>
                          </div>
                          <div>
                            <dt>Publish</dt>
                            <dd>
                              {result.publishMode || "direct-netlify"}
                              {result.githubExport?.path ? ` | ${result.githubExport.path}` : ""}
                            </dd>
                          </div>
                          <div>
                            <dt>Netlify</dt>
                            <dd>
                              {result.deployment?.url ? (
                                <a href={result.deployment.url} target="_blank" rel="noreferrer">
                                  {result.deployment.url}
                                </a>
                              ) : (
                                "No deployment"
                              )}
                            </dd>
                          </div>
                          <div>
                            <dt>Zendesk</dt>
                            <dd>
                              {result.zendesk?.ticketUrl ? (
                                <a href={result.zendesk.ticketUrl} target="_blank" rel="noreferrer">
                                  Ticket {result.zendesk.ticketId}
                                </a>
                              ) : (
                                "No ticket"
                              )}
                            </dd>
                          </div>
                        </dl>

                        {result.stepHistory?.length > 0 && (
                          <details>
                            <summary>Step history</summary>
                            <div className="step-list">
                              {result.stepHistory.map((step, index) => (
                                <div className="step-row" key={`${step.step}-${index}`}>
                                  <span className={`badge ${statusBadgeClass(step.status)}`}>{step.status}</span>
                                  <strong>{step.step}</strong>
                                  <small>{step.provider || "local"} | {step.durationMs} ms</small>
                                </div>
                              ))}
                            </div>
                          </details>
                        )}

                        {result.outreachDraft && (
                          <details>
                            <summary>Outreach draft</summary>
                            <strong>{result.outreachDraft.subject}</strong>
                            <pre>{result.outreachDraft.body}</pre>
                          </details>
                        )}

                        {result.errors?.length > 0 && (
                          <div className="alert alert-danger mt-3 mb-0">
                            {result.errors.map((error) => (
                              <div key={error}>{error}</div>
                            ))}
                          </div>
                        )}
                      </article>
                    ))}
                  </div>
                ) : (
                  !running && (
                    <div className="empty-state">
                      <h3>No pipeline output</h3>
                      <p>Select leads and run the pipeline.</p>
                    </div>
                  )
                )}
              </div>
            </div>
          </section>

          <section className="col-12 col-xl-5">
            <div className="card control-card mb-4 entry-animate">
              <div className="card-header bg-white border-0 pb-0">
                <div className="d-flex align-items-start justify-content-between gap-3">
                  <div>
                    <h2 className="h5 mb-1">Approval Queue</h2>
                    <p className="text-secondary mb-0">Generated pages wait here before Netlify and Zendesk.</p>
                  </div>
                  <span className="badge text-bg-warning">{approvals.filter((approval) => approval.status === "PENDING").length} pending</span>
                </div>
              </div>
              <div className="card-body">
                {approvals.length > 0 ? (
                  <div className="approval-list">
                    {approvals.map((approval) => (
                      <article className="approval-item" key={approval.approvalId}>
                        <div className="d-flex align-items-start justify-content-between gap-2">
                          <div>
                            <h3>{approval.businessName}</h3>
                            <small>{approval.context?.province || approval.context?.location || "South Africa"}</small>
                          </div>
                          <span className={`badge ${statusBadgeClass(approval.status)}`}>{approval.status}</span>
                        </div>
                        <dl>
                          <div>
                            <dt>Approval ID</dt>
                            <dd>{approval.approvalId}</dd>
                          </div>
                          <div>
                            <dt>Owner</dt>
                            <dd>{approval.context?.ownerName || approval.context?.ownerEmail || "Unassigned"}</dd>
                          </div>
                          <div>
                            <dt>Checksum</dt>
                            <dd>{approval.htmlChecksum}</dd>
                          </div>
                          <div>
                            <dt>Publish</dt>
                            <dd>{approval.publishMode || "direct-netlify"}</dd>
                          </div>
                        </dl>
                        {approval.status === "PENDING" && (
                          <div className="d-flex flex-wrap gap-2">
                            <select
                              className="form-select form-select-sm approval-publish-mode"
                              value={publishMode}
                              onChange={(event) => setPublishMode(event.target.value)}
                              disabled={approvalBusy === approval.approvalId}
                              aria-label="Publish mode"
                            >
                              <option value="direct-netlify">Direct Netlify</option>
                              <option value="github-netlify">GitHub + Netlify</option>
                            </select>
                            <button
                              className="btn btn-outline-secondary btn-sm"
                              type="button"
                              onClick={() => previewApproval(approval.approvalId)}
                              disabled={approvalBusy === approval.approvalId || !approval.previewAvailable}
                            >
                              {approvalPreviews[approval.approvalId] ? "Hide Preview" : "Preview"}
                            </button>
                            <button
                              className="btn btn-success btn-sm"
                              type="button"
                              onClick={() => approveSite(approval.approvalId)}
                              disabled={approvalBusy === approval.approvalId}
                            >
                              {approvalBusy === approval.approvalId ? "Working..." : "Approve"}
                            </button>
                            <button
                              className="btn btn-outline-primary btn-sm"
                              type="button"
                              onClick={() => regenerateSite(approval.approvalId)}
                              disabled={approvalBusy === approval.approvalId}
                            >
                              Regenerate
                            </button>
                            <button
                              className="btn btn-outline-danger btn-sm"
                              type="button"
                              onClick={() => rejectSite(approval.approvalId)}
                              disabled={approvalBusy === approval.approvalId}
                            >
                              Reject
                            </button>
                          </div>
                        )}
                        {approvalPreviews[approval.approvalId] && (
                          <iframe
                            className="approval-preview"
                            title={`Preview ${approval.businessName}`}
                            srcDoc={approvalPreviews[approval.approvalId]}
                          />
                        )}
                        {approval.zendesk?.ticketUrl && (
                          <a href={approval.zendesk.ticketUrl} target="_blank" rel="noreferrer">
                            Zendesk ticket
                          </a>
                        )}
                      </article>
                    ))}
                  </div>
                ) : (
                  <div className="empty-state compact">
                    <h3>No approvals yet</h3>
                    <p>Run the pipeline to generate pages for review.</p>
                  </div>
                )}
              </div>
            </div>

            <div className="card control-card mb-4 entry-animate">
              <div className="card-header bg-white border-0 pb-0">
                <h2 className="h5 mb-1">Deployment History</h2>
                <p className="text-secondary mb-0">Every approved Netlify deploy is recorded here.</p>
              </div>
              <div className="card-body">
                {deployments.length > 0 ? (
                  <div className="deployment-list">
                    {deployments.slice(0, 8).map((deployment) => (
                      <article className="deployment-item" key={deployment.id}>
                        <div>
                          <strong>{deployment.site_name || deployment.siteName || deployment.canonical_lead_key}</strong>
                          <span>{deployment.deploy_action || deployment.deployAction} | {deployment.state}</span>
                          <span>{deployment.publishMode || deployment.raw?.publishMode || "direct-netlify"}</span>
                        </div>
                        {deployment.url && (
                          <a href={deployment.url} target="_blank" rel="noreferrer">
                            {deployment.url}
                          </a>
                        )}
                      </article>
                    ))}
                  </div>
                ) : (
                  <div className="empty-state compact">
                    <h3>No deployments</h3>
                    <p>Approvals create or redeploy lead-owned Netlify sites.</p>
                  </div>
                )}
              </div>
            </div>

            <div className="card control-card mb-4 entry-animate">
              <div className="card-header bg-white border-0 pb-0">
                <h2 className="h5 mb-1">Recent Pipeline Runs</h2>
                <p className="text-secondary mb-0">Generation and approval status over time.</p>
              </div>
              <div className="card-body">
                {pipelineRuns.length > 0 ? (
                  <div className="owner-list">
                    {pipelineRuns.slice(0, 8).map((run) => (
                      <div className="owner-row" key={run.pipeline_id}>
                        <strong>{run.status}</strong>
                        <span>{run.pending_count || 0} pending | {run.completed_count || 0} complete | {run.failed_count || 0} failed</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="empty-state compact">
                    <h3>No pipeline runs</h3>
                    <p>Generated pages will appear here after the first run.</p>
                  </div>
                )}
              </div>
            </div>

            {reportingSummary?.ownerPerformance?.length > 0 && (
              <div className="card control-card mb-4 entry-animate">
                <div className="card-header bg-white border-0 pb-0">
                  <h2 className="h5 mb-1">Owner Performance</h2>
                  <p className="text-secondary mb-0">Lead ownership metadata summary.</p>
                </div>
                <div className="card-body">
                  <div className="owner-list">
                    {reportingSummary.ownerPerformance.map((owner) => (
                      <div className="owner-row" key={`${owner.ownerName}-${owner.ownerEmail}-${owner.ownerStatus}`}>
                        <strong>{owner.ownerName}</strong>
                        <span>{owner.ownerStatus} | {owner.leadCount} leads</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            <div className="card control-card entry-animate">
              <div className="card-header bg-white border-0 pb-0">
                <h2 className="h5 mb-1">Live Logs</h2>
                <p className="text-secondary mb-0">Frontend confirmations plus backend background events.</p>
              </div>
              <div className="card-body">
                <div className="log-pane mb-3">
                  <h3>UI Actions</h3>
                  {(uiLogs.length ? uiLogs : [{ id: "empty", level: "info", event: "idle", message: "No UI actions yet.", timestamp: new Date().toISOString() }]).map((log) => (
                    <div className="log-row" key={log.id}>
                      <span className={`badge ${logBadgeClass(log.level)}`}>{log.level}</span>
                      <div>
                        <strong>{log.event}</strong>
                        <p>{log.message}</p>
                      </div>
                    </div>
                  ))}
                </div>
                <div className="log-pane">
                  <h3>Backend Background</h3>
                  {(backendLogs.length ? backendLogs : [{ id: "empty-backend", level: "INFO", event: "idle", message: "No backend logs loaded.", timestamp: new Date().toISOString() }]).map((log) => (
                    <div className="log-row" key={log.id}>
                      <span className={`badge ${logBadgeClass(log.level)}`}>{log.level}</span>
                      <div>
                        <strong>{log.event}</strong>
                        <p>{log.message}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}

export default App;
