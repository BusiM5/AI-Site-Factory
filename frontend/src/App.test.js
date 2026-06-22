import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axios from "axios";
import App from "./App";

jest.mock("axios");
jest.mock("gsap", () => ({
  gsap: {
    fromTo: jest.fn(),
  },
}));

const response = (data, status = 200) =>
  Promise.resolve({
    data,
    status,
    headers: { "x-request-id": "request-1" },
  });

const presetsPayload = {
  presets: [
    { id: "restaurants", label: "Restaurants", industry: "Restaurant", description: "Local restaurants." },
    { id: "plumbers", label: "Plumbers", industry: "Plumbing", description: "Local plumbers." },
  ],
};

const templatesPayload = {
  templates: [{ id: "default-service", name: "Default Service", description: "Default template." }],
};

const debugStatus = {
  status: "READY",
  providers: {
    apify: { configured: true, checks: [{ name: "APIFY_API_TOKEN", configured: true, maskedValue: "api...123" }] },
    github: { configured: true, checks: [{ name: "GITHUB_OWNER", configured: true, maskedValue: "owner" }, { name: "GITHUB_TOKEN", configured: true, maskedValue: "git...ret (13 chars)" }] },
    netlify: { configured: true, checks: [{ name: "NETLIFY_AUTH_TOKEN", configured: true, maskedValue: "net...ret (13 chars)" }] },
  },
  counts: { logsBuffered: 2, githubRepos: 1, gitDeployments: 1 },
};

const debugLogs = {
  logs: [{ id: "log-1", level: "INFO", event: "request.finish", message: "GET /api/debug/status -> 200" }],
};

const reportingSummary = {
  metrics: {
    leadsDiscovered: 1,
    duplicatesSkipped: 0,
    pendingApprovals: 1,
    approvedDeployments: 1,
    githubRepos: 1,
    gitDeployments: 1,
    failedSteps: 0,
    zendeskTickets: 1,
    pipelineRuns: 1,
    activePipelineRuns: 1,
  },
  approvalStatus: { PENDING: 1 },
};

const approval = {
  approvalId: "approval-1",
  pipelineId: "pipeline-1",
  canonicalLeadKey: "canonical-1",
  leadKey: "lead-1",
  businessName: "Alpha Plumbing",
  status: "PENDING",
  htmlChecksum: "abc123",
  previewAvailable: true,
  context: { location: "KwaZulu-Natal, South Africa" },
  githubExport: {
    repository: "owner/ai-site-alpha-plumbing",
    repoUrl: "https://github.com/owner/ai-site-alpha-plumbing",
    commitSha: "commit-1",
  },
  createdAt: new Date().toISOString(),
};

const exportFailedApproval = {
  ...approval,
  approvalId: "approval-export",
  businessName: "Beta Electrical",
  status: "EXPORT_FAILED",
  githubExport: null,
};

const approvalsPayload = { approvals: [approval, exportFailedApproval] };

const deploymentsPayload = {
  deployments: [
    {
      id: "history-1",
      pipeline_id: "pipeline-1",
      site_name: "ai-site-alpha",
      build_id: "build-1",
      deploy_id: "deploy-1",
      deploy_action: "CREATED",
      state: "ready",
      url: "https://alpha.netlify.app",
      github_repo_url: "https://github.com/owner/ai-site-alpha-plumbing",
      github_repo_full_name: "owner/ai-site-alpha-plumbing",
      commit_sha: "commit-1",
      publishMode: "github-netlify",
      deploymentMode: "GitHub \u2192 Netlify",
      deployed_at: new Date().toISOString(),
    },
    {
      id: "history-2",
      pipeline_id: "pipeline-2",
      site_name: "ai-site-beta",
      build_id: null,
      deploy_id: "deploy-2",
      deploy_action: "DIRECT_FALLBACK_CREATED",
      state: "ready",
      url: "https://beta-fallback.netlify.app",
      github_repo_url: "https://github.com/owner/ai-site-beta",
      github_repo_full_name: "owner/ai-site-beta",
      commit_sha: "commit-2",
      publishMode: "direct-netlify-fallback",
      deploymentMode: "Direct Netlify fallback",
      raw: {
        fallbackReason: "Host key verification failed / Could not read from remote repository",
        errors: [{ message: "Git-linked deploy failed before fallback." }],
      },
      deployed_at: new Date().toISOString(),
    },
  ],
};

const pipelineRunsPayload = {
  runs: [
    {
      pipeline_id: "pipeline-1",
      status: "PENDING_APPROVAL",
      pending_count: 1,
      completed_count: 0,
      failed_count: 0,
    },
  ],
};

const pipelineDetailPayload = {
  run: pipelineRunsPayload.runs[0],
  steps: [
    { step: "github_export", status: "COMPLETED", provider: "github", durationMs: 4 },
    { step: "netlify_deploy", status: "COMPLETED", provider: "netlify", durationMs: 7 },
  ],
  approvals: [approval],
};

beforeEach(() => {
  window.history.pushState({}, "", "/");
  axios.mockReset();
  axios.get.mockReset();

  axios.get.mockImplementation((url) => {
    if (url.includes("/api/debug/status")) return response(debugStatus);
    if (url.includes("/api/debug/logs")) return response(debugLogs);
    if (url.includes("/api/reporting/summary")) return response(reportingSummary);
    if (url.includes("/api/approvals/approval-1")) return response({ ...approval, pendingPreviewHtml: "<!doctype html><html><body>Alpha Plumbing</body></html>" });
    if (url.includes("/api/approvals/approval-export")) return response({ ...exportFailedApproval, pendingPreviewHtml: "<!doctype html><html><body>Beta Electrical</body></html>" });
    if (url.includes("/api/approvals")) return response(approvalsPayload);
    if (url.includes("/api/deployments/history")) return response(deploymentsPayload);
    if (url.includes("/api/pipeline/runs/pipeline-1")) return response(pipelineDetailPayload);
    if (url.includes("/api/pipeline/runs")) return response(pipelineRunsPayload);
    return response({});
  });

  axios.mockImplementation(({ method, url, data }) => {
    if (method === "get" && url.includes("/api/approvals/approval-1")) return response({ ...approval, pendingPreviewHtml: "<!doctype html><html><body>Alpha Plumbing</body></html>" });
    if (method === "get" && url.includes("/api/approvals/approval-export")) return response({ ...exportFailedApproval, pendingPreviewHtml: "<!doctype html><html><body>Beta Electrical</body></html>" });
    if (method === "get" && url.includes("/api/pipeline/runs/pipeline-1")) return response(pipelineDetailPayload);
    if (method === "get" && url.includes("/api/pipeline/runs")) return response(pipelineRunsPayload);
    if (method === "get" && url.includes("/api/presets")) return response(presetsPayload);
    if (method === "get" && url.includes("/api/templates")) return response(templatesPayload);

    if (method === "post" && url.includes("/api/leads/discover")) {
      expect(data.limit).toBeGreaterThanOrEqual(1);
      expect(data.limit).toBeLessThanOrEqual(5);
      return response({
        batchId: "batch-1",
        cached: false,
        duplicatesSkipped: 0,
        provinceStats: { "KwaZulu-Natal, South Africa": { selected: 1 } },
        leads: [
          {
            leadKey: "lead-1",
            canonicalLeadKey: "canonical-1",
            businessName: "Alpha Plumbing",
            email: "alpha@example.com",
            phone: "+27 31 000 0000",
            category: "Plumbing",
            location: "KwaZulu-Natal, South Africa",
            sourceUrl: "https://maps.example.com/1",
          },
        ],
        warnings: [],
      });
    }

    if (method === "post" && url.includes("/api/pipeline/run")) {
      return response({
        pipelineId: "pipeline-1",
        status: "PENDING_APPROVAL",
        results: [
          {
            leadKey: "lead-1",
            canonicalLeadKey: "canonical-1",
            businessName: "Alpha Plumbing",
            status: "PENDING_APPROVAL",
            currentStep: "approval",
            approvalStatus: "PENDING",
            pendingApprovalId: "approval-1",
            githubExport: approval.githubExport,
            pendingPreviewHtml: "<!doctype html><html><body>Alpha Plumbing</body></html>",
            stepHistory: [{ step: "github_export", status: "COMPLETED", provider: "github", durationMs: 5 }],
            errors: [],
          },
        ],
      });
    }

    if (method === "post" && url.includes("/api/approvals/approval-1/approve")) {
      return response({
        approvalId: "approval-1",
        status: "APPROVED",
        leadKey: "lead-1",
        canonicalLeadKey: "canonical-1",
        businessName: "Alpha Plumbing",
        deployment: {
          url: "https://alpha.netlify.app",
          buildId: "build-1",
          publishMode: "github-netlify",
          deploymentMode: "GitHub \u2192 Netlify",
          githubRepoUrl: "https://github.com/owner/ai-site-alpha-plumbing",
          githubRepoFullName: "owner/ai-site-alpha-plumbing",
        },
        zendesk: { ticketId: 123, ticketUrl: "https://zendesk.test/123" },
        githubExport: approval.githubExport,
      });
    }

    if (method === "post" && url.includes("/api/approvals/approval-1/retry-export")) {
      return response({ ...approval, status: "PENDING" });
    }

    if (method === "post" && url.includes("/api/approvals/approval-export/retry-export")) {
      return response({ ...exportFailedApproval, status: "PENDING" });
    }

    if (method === "post" && url.includes("/api/approvals/approval-1/reject")) {
      return response({ ...approval, status: "REJECTED" });
    }

    if (method === "post" && url.includes("/api/approvals/approval-1/regenerate")) {
      return response({ ...approval, status: "PENDING" });
    }

    if (method === "post" && url.includes("/api/debug/probe")) {
      return response({
        status: "VALID",
        generatedAt: new Date().toISOString(),
        checks: [{ name: "github", status: "VALID", message: "github check passed.", durationMs: 2, details: {} }],
      });
    }

    if (method === "post" && url.includes("/api/scrape/lead")) {
      return response({ businessName: "Example", email: "info@example.com", domain: "example.com", category: "General Services", location: "South Africa", notes: "Sample lead." });
    }

    if (method === "post" && url.includes("/api/leads/intake")) return response({ leadId: "lead-debug", intakeStatus: "INTAKE_CREATED", validationIssues: [] });
    if (method === "post" && url.includes("/api/leads/lead-debug/clean")) return response({ leadId: "lead-debug", businessName: "Example", email: "info@example.com", domain: "example.com", category: "General Services", location: "South Africa", sourceRef: "ui-debugger", cleanSummary: "Sample lead.", cleanStatus: "CLEAN", validationIssues: [] });
    if (method === "post" && url.includes("/api/content/generate")) return response({ contentPacket: { headline: "Example", summary: "Example summary", serviceBlocks: [], CTA: "Contact", tone: "professional", brandNotes: "Debug" }, outreachDraft: { subject: "Website preview for Example", body: "Preview body", recipientEmail: "info@example.com", approvalStatus: "Pending Review" }, generationStatus: "GENERATED", generatedAt: new Date().toISOString() });
    if (method === "post" && url.includes("/api/site/build-preview")) return response({ previewUrl: "https://preview.ai-site-factory.local/lead-debug", deploymentStatus: "PREVIEW_READY", buildReference: "build-1", generatedAt: new Date().toISOString(), reviewStatus: "PENDING_REVIEW", previewType: "SIMULATED_PREVIEW_REFERENCE", limitationNote: "Debug preview." });
    if (method === "get" && url.includes("/api/leads/lead-debug")) return response({ intakeStatus: "INTAKE_CREATED" });
    if (method === "post" && url.includes("/api/outreach/generate")) return response({ subject: "Website preview for Example", body: "Preview body", recipientEmail: "info@example.com", status: "DRAFT_GENERATED" });

    return response({});
  });
});

test("renders four-page Pipeline Workspace with merged sections", async () => {
  render(<App />);

  expect(await screen.findByText("AI Site Factory")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Pipeline Workspace" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Deployments" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Pipeline Runs" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Admin/Backend" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Dashboard" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Lead Discovery" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Generate & Approval" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
  expect(screen.getByText("GitHub to Netlify")).toBeInTheDocument();
  expect(screen.getByText("Lead Pipeline Control Center")).toBeInTheDocument();
  [
    "Section 1: Lead Discovery",
    "Section 2: Selected Leads",
    "Section 3: Generate Landing Page",
    "Section 4: Approval Queue",
    "Section 5: Preview",
    "Section 6: Deployment Action",
  ].forEach((heading) => expect(screen.getByText(heading)).toBeInTheDocument());
  expect(screen.queryByText("All owners")).not.toBeInTheDocument();
  expect(screen.queryByText("Owner Performance")).not.toBeInTheDocument();
  expect(screen.queryByText("Assign Owner")).not.toBeInTheDocument();
});

test("discovers leads with selectable count and runs GitHub export pipeline", async () => {
  render(<App />);

  fireEvent.click(await screen.findByRole("link", { name: "Pipeline Workspace" }));
  fireEvent.change(screen.getByLabelText("Lead count"), { target: { value: "5" } });
  fireEvent.click(screen.getByText("Search Leads"));

  expect(await screen.findByText("Alpha Plumbing")).toBeInTheDocument();
  expect(screen.getByText("LIVE")).toBeInTheDocument();
  expect(screen.queryByText(/All provinces/i)).not.toBeInTheDocument();

  fireEvent.click(screen.getByLabelText("Select Alpha Plumbing"));
  fireEvent.click(screen.getByText("Run Pipeline"));

  expect(await screen.findByText("approval-1")).toBeInTheDocument();
  expect(screen.getAllByText("owner/ai-site-alpha-plumbing").length).toBeGreaterThan(0);
  expect(screen.getAllByText("commit-1").length).toBeGreaterThan(0);
});

test("previews and approves a GitHub-based Netlify deployment", async () => {
  render(<App />);

  fireEvent.click(await screen.findByRole("link", { name: "Pipeline Workspace" }));
  expect(await screen.findByText("approval-1")).toBeInTheDocument();

  const queue = screen.getByText("Section 4: Approval Queue").closest("section");
  fireEvent.click(within(queue).getAllByText("Preview")[0]);
  expect(await screen.findByTitle("Preview Alpha Plumbing")).toBeInTheDocument();

  fireEvent.click(within(queue).getAllByText("Approve")[0]);
  expect(await screen.findByText(/Approved and deployed Alpha Plumbing/i)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("link", { name: "Deployments" }));
  expect(await screen.findByText("https://alpha.netlify.app")).toBeInTheDocument();
  expect(screen.getByText("build-1")).toBeInTheDocument();
  expect(screen.getByText("owner/ai-site-alpha-plumbing")).toBeInTheDocument();
  expect(screen.getAllByText("GitHub \u2192 Netlify").length).toBeGreaterThan(0);
});

test("supports retry export, regenerate, and reject actions from approval queue", async () => {
  render(<App />);

  fireEvent.click(await screen.findByRole("link", { name: "Pipeline Workspace" }));
  expect(await screen.findByText("approval-export")).toBeInTheDocument();

  fireEvent.click(screen.getByText("Retry Export"));
  expect(await screen.findByText(/GitHub export ready for Beta Electrical/i)).toBeInTheDocument();

  fireEvent.click(screen.getAllByText("Regenerate")[0]);
  expect(await screen.findByText(/Regenerated Alpha Plumbing/i)).toBeInTheDocument();

  fireEvent.click(screen.getAllByText("Reject")[0]);
  expect(await screen.findByText(/Rejected Alpha Plumbing/i)).toBeInTheDocument();
});

test("shows deployments, pipeline run details, and merged backend admin settings", async () => {
  render(<App />);

  fireEvent.click(await screen.findByRole("link", { name: "Deployments" }));
  expect(await screen.findByText("https://alpha.netlify.app")).toBeInTheDocument();
  expect(screen.getByText("owner/ai-site-alpha-plumbing")).toBeInTheDocument();
  expect(screen.getByText("https://beta-fallback.netlify.app")).toBeInTheDocument();
  expect(screen.getByText("Direct Netlify fallback")).toBeInTheDocument();
  expect(screen.getAllByText(/Git-linked deploy failed before fallback/i).length).toBeGreaterThan(0);
  expect(screen.getAllByText("View technical details").length).toBeGreaterThan(0);

  fireEvent.click(await screen.findByRole("link", { name: "Pipeline Runs" }));
  fireEvent.click(await screen.findByText("pipeline-1"));

  expect(await screen.findByText("github_export")).toBeInTheDocument();
  expect(screen.getByText("netlify_deploy")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("link", { name: "Admin/Backend" }));
  expect(await screen.findByText("API Safety Center")).toBeInTheDocument();
  expect(screen.getByText("Workspace Settings")).toBeInTheDocument();
  expect(screen.getByText("Provider Diagnostics")).toBeInTheDocument();
  expect(screen.getByText("Model & API Usage")).toBeInTheDocument();
  expect(screen.getByLabelText("Max leads per search")).toHaveValue("3");
  expect(screen.getByText("Gemini images")).toBeInTheDocument();
  expect(screen.getByText("Off")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Local Probe"));
  expect(await screen.findByText("github check passed.")).toBeInTheDocument();
});
