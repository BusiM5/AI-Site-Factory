import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axios from "axios";
import App from "./App";

jest.mock("axios");
jest.mock("bootstrap", () => ({
  Tooltip: jest.fn().mockImplementation(() => ({ dispose: jest.fn() })),
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
        leads: [
          {
            leadKey: "lead-1",
            businessName: "Alpha Plumbing",
            email: "alpha@example.com",
            category: "Plumbing",
            location: "Durban",
            sourceUrl: "https://maps.example.com/1",
          },
        ],
        warnings: [],
      });
    }

    if (method === "post" && url.includes("/api/pipeline/run")) {
      return response({
        pipelineId: "pipeline-1",
        status: "COMPLETED",
        results: [
          {
            leadKey: "lead-1",
            businessName: "Alpha Plumbing",
            status: "COMPLETED",
            deployment: { url: "https://alpha.netlify.app" },
            zendesk: {
              ticketId: 123,
              ticketUrl: "https://example.zendesk.com/agent/tickets/123",
            },
            outreachDraft: {
              subject: "Website preview for Alpha Plumbing",
              body: "Please see https://alpha.netlify.app",
            },
            errors: [],
          },
        ],
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
});

test("discovers leads, selects one, and runs pipeline", async () => {
  renderApp();

  fireEvent.click(await screen.findByText("Search Leads"));
  expect(await screen.findByText("Alpha Plumbing")).toBeInTheDocument();

  fireEvent.click(screen.getByLabelText("Select Alpha Plumbing"));
  fireEvent.click(screen.getByText("Run Pipeline"));

  await waitFor(() => {
    expect(screen.getByText("pipeline-1")).toBeInTheDocument();
  });

  expect(screen.getByText("https://alpha.netlify.app")).toBeInTheDocument();
  expect(screen.getByText("Ticket 123")).toBeInTheDocument();
});

test("runs safe local API flow debugger", async () => {
  renderApp();

  fireEvent.click(await screen.findByText("Safe Flow Test"));

  await waitFor(() => {
    expect(screen.getByText(/Safe local API flow test passed/i)).toBeInTheDocument();
  });

  expect(screen.getByText(/lead-debug passed scrape -> intake -> clean/i)).toBeInTheDocument();
});
