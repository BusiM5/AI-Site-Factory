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
  const API_BASE = process.env.REACT_APP_API_BASE || "http://127.0.0.1:8001";
  const shellRef = useRef(null);
  const flowRef = useRef(null);

  const [presets, setPresets] = useState(FALLBACK_PRESETS);
  const [templates, setTemplates] = useState(FALLBACK_TEMPLATES);
  const [selectedPresetId, setSelectedPresetId] = useState("restaurants");
  const [selectedTemplateId, setSelectedTemplateId] = useState("default-service");
  const [location, setLocation] = useState("South Africa");
  const [customQuery, setCustomQuery] = useState("");
  const [batchId, setBatchId] = useState(null);
  const [leads, setLeads] = useState([]);
  const [selectedLeadKeys, setSelectedLeadKeys] = useState([]);
  const [warnings, setWarnings] = useState([]);
  const [pipelineResult, setPipelineResult] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState("info");
  const [debugStatus, setDebugStatus] = useState(null);
  const [backendLogs, setBackendLogs] = useState([]);
  const [uiLogs, setUiLogs] = useState([]);
  const [apiProbe, setApiProbe] = useState(null);
  const [manualFlow, setManualFlow] = useState(null);
  const [debugBusy, setDebugBusy] = useState(null);

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
  }, [callApi, refreshDiagnostics, setNotice]);

  useEffect(() => {
    const interval = setInterval(() => refreshDiagnostics(true), 7000);
    return () => clearInterval(interval);
  }, [refreshDiagnostics]);

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

  const completedCount =
    pipelineResult?.results?.filter((result) =>
      result.status?.startsWith("COMPLETED")
    ).length || 0;

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
        label: "Pipeline",
        detail: running ? "Models active" : pipelineResult?.status || "Not run",
        state: running ? "active" : pipelineResult ? (failedCount ? "danger" : "complete") : "idle",
      },
      {
        key: "deploy",
        label: "Deploy",
        detail: completedCount ? "Sites created" : "Pending",
        state: completedCount ? "complete" : running ? "active" : "idle",
      },
      {
        key: "outreach",
        label: "Outreach",
        detail: completedCount ? "Tickets/drafts ready" : "Pending",
        state: completedCount ? "complete" : running ? "active" : "idle",
      },
    ],
    [completedCount, debugStatus, discovering, failedCount, leads.length, pipelineResult, running, selectedLeads.length]
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
    setSelectedLeadKeys(leads.map((lead) => lead.leadKey));
    addUiLog("info", "leads.select_all", "All loaded leads selected.", { count: leads.length });
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
      setSelectedLeadKeys([]);
      setNotice("Searching Google Maps with Apify...", "info");

      const data = await callApi(
        "Lead Discovery API",
        "post",
        "/api/leads/discover",
        {
          presetId: selectedPreset.id,
          location,
          query: customQuery || null,
          limit: 10,
        },
        { timeout: 240000 }
      );

      setBatchId(data.batchId);
      setLeads(data.leads || []);
      setWarnings(data.warnings || []);
      setNotice(`Fetched ${data.leads?.length || 0} leads.`, data.leads?.length ? "success" : "warning");
      refreshDiagnostics(true);
    } catch (error) {
      const failure = formatApiError(error);
      setLeads([]);
      setBatchId(null);
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
      setNotice("Running enrichment, image generation, Netlify deployment, and Zendesk sync...", "info");

      const data = await callApi(
        "Full Pipeline API",
        "post",
        "/api/pipeline/run",
        {
          sourceBatchId: batchId,
          templateId: selectedTemplate.id,
          leads: selectedLeads,
        },
        { timeout: 420000 }
      );

      setPipelineResult(data);
      setNotice(`Pipeline finished with status ${data.status}.`, data.status === "COMPLETED" ? "success" : "warning");
      refreshDiagnostics(true);
    } catch (error) {
      const failure = formatApiError(error);
      setNotice(failure.message || "Pipeline run failed. Check debug logs for the failed provider.", "danger");
      refreshDiagnostics(true);
    } finally {
      setRunning(false);
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
    if (status === "PROCESSING" || status === "ACTION_REQUIRED") return "text-bg-warning";
    if (status === "FAILED" || status === "INVALID") return "text-bg-danger";
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
                <span>{completedCount}</span>
                <label>Complete</label>
              </div>
              <div className="metric-card">
                <span>{backendLogs.length}</span>
                <label>Logs</label>
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
                    <label className="form-label">Location</label>
                    <input
                      className="form-control"
                      value={location}
                      onChange={(event) => setLocation(event.target.value)}
                      placeholder="South Africa"
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
                </div>

                <div className="template-callout mt-3">
                  <strong>{selectedTemplate?.name}</strong>
                  <span>{selectedTemplate?.description}</span>
                </div>

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
                <p className="text-secondary mb-0">{batchId ? `Batch ${batchId}` : "No batch loaded"}</p>
              </div>
              <div className="d-flex flex-wrap gap-2">
                <button className="btn btn-outline-secondary" type="button" onClick={selectAllLeads} disabled={!leads.length}>
                  Select All
                </button>
                <button className="btn btn-outline-secondary" type="button" onClick={clearSelectedLeads} disabled={!selectedLeadKeys.length}>
                  Clear
                </button>
                <button
                  className="btn btn-primary"
                  onClick={runPipeline}
                  disabled={running || !selectedLeads.length}
                  data-bs-toggle="tooltip"
                  title="Runs enrichment, copy generation, image generation, Netlify deployment, Groq outreach, and Zendesk ticket creation."
                >
                  {running ? "Running..." : "Run Pipeline"}
                </button>
              </div>
            </div>
          </div>
          <div className="card-body">
            {leads.length > 0 ? (
              <div className="table-responsive data-table-wrap">
                <table className="table align-middle mb-0">
                  <thead>
                    <tr>
                      <th>Select</th>
                      <th>Business</th>
                      <th>Contact</th>
                      <th>Category</th>
                      <th>Rating</th>
                      <th>Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {leads.map((lead) => (
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
                        <p>Queued for enrichment, deployment, outreach, and Zendesk sync.</p>
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
            <div className="card control-card h-100 entry-animate">
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
