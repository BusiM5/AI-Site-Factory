import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axios from "axios";
import App from "./App";

jest.mock("axios");
jest.mock("bootstrap", () => ({
  Tooltip: Object.assign(
    jest.fn().mockImplementation(() => ({ dispose: jest.fn() })),
    { getInstance: jest.fn(() => null) }
  ),
}));
jest.mock("gsap", () => ({
  gsap: {
    fromTo: jest.fn(),
    to: jest.fn(),
    killTweensOf: jest.fn(),
  },
}));

const presets = [
  {
    id: "restaurants",
    label: "Restaurants",
    industry: "Restaurant",
    description: "Local restaurants and cafes.",
  },
  {
    id: "plumbers",
    label: "Plumbers",
    industry: "Plumbing",
    description: "Local plumbing services.",
  },
];

const templates = [
  {
    id: "default-service",
    name: "Default Service",
    description: "Default template.",
  },
];

const debugStatus = {
  status: "READY",
  providers: {
    apify: { configured: true, checks: [{ name: "APIFY_API_TOKEN", configured: true }] },
    gemini: { configured: true, checks: [{ name: "GEMINI_API_KEY", configured: true }] },
  },
  counts: { logsBuffered: 2 },
};

const debugLogs = {
  logs: [
    {
      id: "log-1",
      level: "INFO",
      event: "request.finish",
      message: "GET /api/debug/status -> 200",
      timestamp: new Date().toISOString(),
    },
  ],
};

const reportingSummary = {
  metrics: {
    leadsDiscovered: 1,
    duplicatesSkipped: 0,
    pendingApprovals: 1,
    approvedDeployments: 1,
    failedSteps: 0,
    zendeskTickets: 1,
    pipelineRuns: 1,
    activePipelineRuns: 1,
  },
  ownerPerformance: [
    {
      ownerName: "Ops",
      ownerEmail: "ops@example.com",
      ownerStatus: "assigned",
      leadCount: 1,
    },
  ],
  approvalStatus: { PENDING: 1 },
};

const approvalsPayload = {
  approvals: [
    {
      approvalId: "approval-1",
      pipelineId: "pipeline-1",
      canonicalLeadKey: "canonical-1",
      leadKey: "lead-1",
      businessName: "Alpha Plumbing",
      status: "PENDING",
      htmlChecksum: "abc123",
      previewAvailable: true,
      context: {
        province: "KwaZulu-Natal",
        location: "KwaZulu-Natal, South Africa",
        ownerName: "Ops",
      },
    },
  ],
};

const deploymentsPayload = {
  deployments: [
    {
      id: "history-1",
      site_name: "ai-site-alpha",
      deploy_action: "CREATED",
      state: "ready",
      url: "https://alpha.netlify.app",
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

const response = (data, status = 200) =>
  Promise.resolve({
    data,
    status,
    headers: { "x-request-id": "request-1" },
  });

const renderApp = () => {
  return render(<App />);
};

beforeEach(() => {
  axios.mockReset();
  axios.get.mockReset();

  axios.get.mockImplementation((url) => {
    if (url.includes("/api/debug/status")) return response(debugStatus);
    if (url.includes("/api/debug/logs")) return response(debugLogs);
    if (url.includes("/api/reporting/summary")) return response(reportingSummary);
    if (url.includes("/api/approvals")) return response(approvalsPayload);
    if (url.includes("/api/deployments/history")) return response(deploymentsPayload);
    if (url.includes("/api/pipeline/runs")) return response(pipelineRunsPayload);
    return response({});
  });

  axios.mockImplementation(({ method, url }) => {
    if (method === "get" && url.includes("/api/presets")) {
      return response({ presets });
    }

    if (method === "get" && url.includes("/api/templates")) {
      return response({ templates });
    }

    if (method === "post" && url.includes("/api/leads/discover")) {
      return response({
        batchId: "batch-1",
        duplicatesSkipped: 0,
        provinceStats: {
          "KwaZulu-Natal": { selected: 1, duplicatesSkipped: 0 },
        },
        leads: [
          {
            leadKey: "lead-1",
            canonicalLeadKey: "canonical-1",
            businessName: "Alpha Plumbing",
            email: "alpha@example.com",
            category: "Plumbing",
            location: "KwaZulu-Natal, South Africa",
            province: "KwaZulu-Natal",
            ownerName: "Ops",
            ownerStatus: "assigned",
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
            pipelineStatus: "PENDING_APPROVAL",
            currentStep: "approval",
            approvalStatus: "PENDING",
            pendingApprovalId: "approval-1",
            stepHistory: [
              {
                step: "gemini_page_prompt",
                status: "COMPLETED",
                provider: "gemini",
                durationMs: 5,
              },
            ],
            deployment: null,
            zendesk: null,
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
          deployAction: "CREATED",
          siteCreated: true,
          siteReused: false,
        },
        zendesk: {
          ticketId: 123,
          ticketUrl: "https://example.zendesk.com/agent/tickets/123",
        },
      });
    }

    if (method === "get" && url.includes("/api/approvals/approval-1")) {
      return response({
        ...approvalsPayload.approvals[0],
        pendingPreviewHtml: "<!doctype html><html><body><h1>Alpha Plumbing</h1></body></html>",
      });
    }

    if (method === "post" && url.includes("/api/leads/canonical-1/owner")) {
      return response({
        canonicalLeadKey: "canonical-1",
        businessName: "Alpha Plumbing",
        ownerName: "Ops",
        ownerEmail: "ops@example.com",
        ownerStatus: "working",
        assignedAt: new Date().toISOString(),
      });
    }

    if (method === "post" && url.includes("/api/debug/probe")) {
      return response({
        status: "VALID",
        generatedAt: new Date().toISOString(),
        checks: [
          {
            name: "environment",
            status: "VALID",
            message: "environment check passed.",
            durationMs: 2,
            details: {},
          },
        ],
      });
    }

    if (method === "post" && url.includes("/api/scrape/lead")) {
      return response({
        businessName: "Example",
        email: "info@example.com",
        domain: "example.com",
        category: "General Services",
        location: "South Africa",
        notes: "Sample lead.",
      });
    }

    if (method === "post" && url.includes("/api/leads/intake")) {
      return response({ leadId: "lead-debug", intakeStatus: "INTAKE_CREATED", validationIssues: [] });
    }

    if (method === "post" && url.includes("/api/leads/lead-debug/clean")) {
      return response({
        leadId: "lead-debug",
        businessName: "Example",
        email: "info@example.com",
        domain: "example.com",
        category: "General Services",
        location: "South Africa",
        sourceRef: "ui-debugger",
        cleanSummary: "Sample lead.",
        cleanStatus: "CLEAN",
        validationIssues: [],
      });
    }

    if (method === "post" && url.includes("/api/content/generate")) {
      return response({
        contentPacket: {
          headline: "Example",
          summary: "Example summary",
          serviceBlocks: [],
          CTA: "Contact",
          tone: "professional",
          brandNotes: "Debug",
        },
        outreachDraft: {
          subject: "Website preview for Example",
          body: "Preview body",
          recipientEmail: "info@example.com",
          approvalStatus: "Pending Review",
        },
        generationStatus: "GENERATED",
        generatedAt: new Date().toISOString(),
      });
    }

    if (method === "post" && url.includes("/api/site/build-preview")) {
      return response({
        previewUrl: "https://preview.ai-site-factory.local/lead-debug",
        deploymentStatus: "PREVIEW_READY",
        buildReference: "build-1",
        generatedAt: new Date().toISOString(),
        reviewStatus: "PENDING_REVIEW",
        previewType: "SIMULATED_PREVIEW_REFERENCE",
        limitationNote: "Debug preview.",
      });
    }

    if (method === "get" && url.includes("/api/leads/lead-debug")) {
      return response({ intakeStatus: "INTAKE_CREATED" });
    }

    if (method === "post" && url.includes("/api/outreach/generate")) {
      return response({
        subject: "Website preview for Example",
        body: "Preview body",
        recipientEmail: "info@example.com",
        status: "DRAFT_GENERATED",
      });
    }

    return response({});
  });
});

test("renders lead pipeline dashboard", async () => {
  renderApp();

  expect(await screen.findByRole("heading", { name: /Lead Pipeline Control Center/i })).toBeInTheDocument();
  expect(screen.getByText("Restaurants")).toBeInTheDocument();
  expect(screen.getAllByText("Default Service").length).toBeGreaterThan(0);
  expect(screen.getByText("API Safety Center")).toBeInTheDocument();
  expect(screen.getByText("Approval Queue")).toBeInTheDocument();
});

test("discovers leads, selects one, runs pipeline, and approves deployment", async () => {
  renderApp();

  fireEvent.click(await screen.findByText("Search Leads"));
  expect(await screen.findByText("Alpha Plumbing")).toBeInTheDocument();
  expect(screen.getAllByText("KwaZulu-Natal").length).toBeGreaterThan(0);

  fireEvent.click(screen.getByText("Assign"));
  await waitFor(() => {
    expect(screen.getByText(/Owner updated for Alpha Plumbing/i)).toBeInTheDocument();
  });

  fireEvent.click(screen.getByLabelText("Select Alpha Plumbing"));
  fireEvent.click(screen.getByText("Resume Pipeline"));

  await waitFor(() => {
    expect(screen.getByText("pipeline-1")).toBeInTheDocument();
  });

  expect(screen.getAllByText("approval-1").length).toBeGreaterThan(0);
  expect(screen.getByText(/Step history/i)).toBeInTheDocument();

  fireEvent.click(screen.getByText("Preview"));
  await waitFor(() => {
    expect(screen.getByTitle("Preview Alpha Plumbing")).toBeInTheDocument();
  });

  fireEvent.click(screen.getByText("Approve"));

  await waitFor(() => {
    expect(screen.getByText(/Approved and deployed Alpha Plumbing/i)).toBeInTheDocument();
  });

  expect(screen.getByText("https://alpha.netlify.app")).toBeInTheDocument();
});

test("runs safe local API flow debugger", async () => {
  renderApp();

  fireEvent.click(await screen.findByText("Safe Flow Test"));

  await waitFor(() => {
    expect(screen.getByText(/Safe local API flow test passed/i)).toBeInTheDocument();
  });

  expect(screen.getByText(/lead-debug passed scrape -> intake -> clean/i)).toBeInTheDocument();
});
