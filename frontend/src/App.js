import { useState } from "react";
import axios from "axios";
import "./App.css";

function App() {
  const API_BASE =
    process.env.REACT_APP_API_BASE || "http://127.0.0.1:8000";

  const [websiteUrl, setWebsiteUrl] = useState("");
  const [lead, setLead] = useState({});
  const [leadId, setLeadId] = useState(null);
  const [cleaned, setCleaned] = useState(null);
  const [generation, setGeneration] = useState(null);
  const [previewBuild, setPreviewBuild] = useState(null);
  const [reviewStatus, setReviewStatus] = useState("Pending Review");
  const [zendeskResult, setZendeskResult] = useState(null);

  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [errors, setErrors] = useState({});

  const contentPacket = generation?.contentPacket;

  const handleChange = (e) => {
    setLead({ ...lead, [e.target.name]: e.target.value });
    setMessage("");
    setErrors({});
  };

  const validateLead = () => {
    const newErrors = {};

    if (!lead.businessName?.trim()) {
      newErrors.businessName = "Business name is required.";
    }

    if (!lead.email?.trim()) {
      newErrors.email = "Email address is required.";
    } else if (!lead.email.includes("@")) {
      newErrors.email = "Please enter a valid email address.";
    }

    if (!lead.category?.trim()) {
      newErrors.category = "Category / industry is required.";
    }

    setErrors(newErrors);

    if (Object.keys(newErrors).length > 0) {
      setMessage("Please fix the highlighted fields.");
      return false;
    }

    setMessage("Lead is valid.");
    return true;
  };

  const loadSampleLead = () => {
    setLead({
      businessName: "FixIt Plumbing",
      email: "info@fixitplumbing.co.za",
      domain: "fixitplumbing.co.za",
      category: "Plumbing",
      location: "Durban",
      notes:
        "Provides emergency plumbing, pipe repairs, leak detection, and residential plumbing services.",
    });

    setWebsiteUrl("");
    setLeadId(null);
    setCleaned(null);
    setGeneration(null);
    setPreviewBuild(null);
    setErrors({});
    setMessage("Sample lead loaded.");
  };

  const fetchLeadFromWebsite = async () => {
    if (!websiteUrl.trim()) {
      setMessage("Please enter a website URL.");
      return;
    }

    try {
      setLoading(true);
      setMessage("Fetching lead data from website source...");

      const response = await axios.post(`${API_BASE}/api/scrape/lead`, {
        url: websiteUrl,
      });

      const data = response.data;

      setLead({
        businessName: data.businessName || "",
        email: data.email || "",
        domain: data.domain || websiteUrl,
        category: data.category || "",
        location: data.location || "",
        notes: data.notes || "",
      });

      setLeadId(null);
      setCleaned(null);
      setGeneration(null);
      setPreviewBuild(null);
      setErrors({});
      setMessage("Lead data fetched into intake form.");
    } catch (error) {
      console.error(error);
      setMessage("Failed to fetch lead data from scraper API.");
    } finally {
      setLoading(false);
    }
  };

  const submitIntakeAndClean = async () => {
    if (!validateLead()) return;

    try {
      setLoading(true);
      setMessage("Creating lead intake record...");

      const intakeResponse = await axios.post(`${API_BASE}/api/leads/intake`, {
        rawLeadRow: {
          businessName: lead.businessName,
          email: lead.email,
          domain: lead.domain || websiteUrl || "manual-entry",
          category: lead.category,
          location: lead.location || "Not provided",
          notes: lead.notes || "No additional notes provided.",
        },
        sourceType: websiteUrl ? "scraper-demo" : "manual",
        batchId: "phase-1-demo",
      });

      const newLeadId = intakeResponse.data.leadId;
      setLeadId(newLeadId);

      setMessage("Lead intake created. Cleaning lead record...");

      const cleanResponse = await axios.post(
        `${API_BASE}/api/leads/${newLeadId}/clean`
      );

      setCleaned(cleanResponse.data);
      setGeneration(null);
      setPreviewBuild(null);

      setMessage("Lead intake and cleaning completed.");
    } catch (error) {
      console.error(error);
      setMessage("Lead intake or cleaning failed.");
    } finally {
      setLoading(false);
    }
  };

  const generateContent = async () => {
    if (!cleaned) {
      setMessage("Clean the lead before generating content.");
      return;
    }

    try {
      setLoading(true);
      setMessage("Generating content packet...");

      const response = await axios.post(`${API_BASE}/api/content/generate`, {
        leadRecord: cleaned,
        generationProfile: "default",
        templateId: "standard-service-template",
        toneProfile: "professional",
      });

      setGeneration(response.data);
      setPreviewBuild(null);

      setMessage("Content packet generated.");
    } catch (error) {
      console.error(error);
      setMessage("Content generation failed.");
    } finally {
      setLoading(false);
    }
  };

  const buildPreview = async () => {
    if (!leadId || !contentPacket) {
      setMessage("Generate the content packet before building a preview.");
      return;
    }

    try {
      setLoading(true);
      setMessage("Building preview website...");

      const response = await axios.post(`${API_BASE}/api/site/build-preview`, {
        leadId,
        contentPacket,
        templateId: "standard-service-template",
        deployMode: "preview",
      });

      setPreviewBuild(response.data);
      setMessage("Preview build completed.");
    } catch (error) {
      console.error(error);
      setMessage("Preview build failed.");
    } finally {
      setLoading(false);
    }
  };

  const copyContentPacket = () => {
    if (!generation) return;

    navigator.clipboard.writeText(JSON.stringify(generation, null, 2));
    setMessage("Generated content packet copied.");
  };

  const downloadPreview = () => {
    if (!contentPacket) return;

    const html = `
<!DOCTYPE html>
<html>
<head>
  <title>${contentPacket.headline}</title>
</head>
<body>
  <h1>${contentPacket.headline}</h1>
  <p>${contentPacket.summary}</p>

  <h2>Services</h2>
  <ul>
    ${contentPacket.serviceBlocks
      .map(
        (service) =>
          `<li><strong>${service.title}</strong>: ${service.description}</li>`
      )
      .join("")}
  </ul>

  <p><strong>${contentPacket.CTA}</strong></p>
</body>
</html>
`;

    const blob = new Blob([html], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");

    link.href = url;
    link.download = "phase-1-preview.html";
    link.click();

    URL.revokeObjectURL(url);
  };

  const resetApp = () => {
    setWebsiteUrl("");
    setLead({});
    setLeadId(null);
    setCleaned(null);
    setGeneration(null);
    setPreviewBuild(null);
    setZendeskResult(null);
    setLoading(false);
    setMessage("");
    setErrors({});
  };

 const approvePreview = async () => {
  if (!previewBuild || !cleaned) {
    setMessage("Build the preview before approving.");
    return;
  }

  try {
    setLoading(true);

 const response = await axios.post(
      `${API_BASE}/api/zendesk/sync-lead`,
      {
        leadId: cleaned.leadId,
        businessName: cleaned.businessName,
        email: cleaned.email,
        category: cleaned.category,
        previewReference: previewBuild.previewUrl,
        approvalStatus: "Approved",
      }
    );

    setZendeskResult(response.data);
    setReviewStatus("Approved");

    setMessage(
      "Preview approved and synced to Zendesk successfully."
    );
  } catch (error) {
    console.error(error);

    setMessage(
      "Preview approved but Zendesk sync failed."
    );
  } finally {
    setLoading(false);
  }
};

const requestRegeneration = () => {
  setReviewStatus("Regeneration Requested");
  setGeneration(null);
  setPreviewBuild(null);
  setZendeskResult(null);
  setMessage("Regeneration requested. Generate a new content packet.");
};

  return (
    <div className="app">
      <header className="header">
        <span className="badge">Phase 1 Lead-to-Preview Backbone</span>
        <h1>AI Site Factory</h1>
        <p>
          Documentation-aligned workflow: lead source, intake, cleaning,
          generation, and preview build.
        </p>
      </header>

      {message && <div className="message">{message}</div>}

      <div className="layout">
        <div className="left-panel">
          <div className="progress">
            <div className={!leadId ? "step active" : "step done"}>
              1. Intake
            </div>
            <div className={cleaned && !generation ? "step active" : cleaned ? "step done" : "step"}>
              2. Clean
            </div>
            <div className={generation && !previewBuild ? "step active" : generation ? "step done" : "step"}>
              3. Generate
            </div>
            <div className={previewBuild ? "step done" : "step"}>
              4. Preview
            </div>
          </div>

          <section className="card">
            <div className="scraper-box">
              <h3>Lead Source / Scraper</h3>
              <p className="helper">
                Enter a public website URL to fetch structured demo lead data.
              </p>

              <input
                type="text"
                placeholder="Enter website URL"
                value={websiteUrl}
                onChange={(e) => setWebsiteUrl(e.target.value)}
              />

              <button onClick={fetchLeadFromWebsite} disabled={loading}>
                {loading ? "Fetching..." : "Fetch Lead From Website"}
              </button>
            </div>

            <h2>Lead Intake</h2>
            <p className="helper">
              Review or enter the raw lead before creating the intake record.
            </p>

            <input
              name="businessName"
              placeholder="Business Name *"
              value={lead.businessName || ""}
              onChange={handleChange}
            />
            {errors.businessName && (
              <p className="error">{errors.businessName}</p>
            )}

            <input
              name="email"
              placeholder="Email Address *"
              value={lead.email || ""}
              onChange={handleChange}
            />
            {errors.email && <p className="error">{errors.email}</p>}

            <input
              name="domain"
              placeholder="Domain / Website"
              value={lead.domain || ""}
              onChange={handleChange}
            />

            <input
              name="category"
              placeholder="Category / Industry *"
              value={lead.category || ""}
              onChange={handleChange}
            />
            {errors.category && <p className="error">{errors.category}</p>}

            <input
              name="location"
              placeholder="Location"
              value={lead.location || ""}
              onChange={handleChange}
            />

            <textarea
              name="notes"
              placeholder="Business Notes"
              value={lead.notes || ""}
              onChange={handleChange}
            ></textarea>

            <div className="button-row">
              <button onClick={loadSampleLead} className="sample-btn">
                Load Sample Lead
              </button>

              <button onClick={validateLead} className="secondary-btn">
                Validate Lead
              </button>

              <button onClick={submitIntakeAndClean} disabled={loading}>
                {loading ? "Processing..." : "Create Intake & Clean"}
              </button>
            </div>
          </section>
        </div>

        <div className="right-panel">
          {cleaned ? (
            <section className="card">
              <h2>Cleaned Lead Record</h2>
              <p className="helper">
                Output from <strong>/api/leads/&#123;leadId&#125;/clean</strong>.
              </p>

              <pre>{JSON.stringify(cleaned, null, 2)}</pre>

              <button onClick={generateContent} disabled={loading}>
                {loading ? "Generating..." : "Generate Content Packet"}
              </button>
            </section>
          ) : (
            <section className="card empty-state">
              <h2>Cleaned Lead Record</h2>
              <p>Create the lead intake and cleaning output to continue.</p>
            </section>
          )}

          {generation ? (
            <section className="card">
              <h2>Generated Content Packet</h2>
              <p className="helper">
                Output from <strong>/api/content/generate</strong>.
              </p>

              <pre>{JSON.stringify(generation, null, 2)}</pre>

              <button onClick={buildPreview} disabled={loading}>
                {loading ? "Building..." : "Build Preview"}
              </button>
            </section>
          ) : (
            <section className="card empty-state">
              <h2>Generated Content Packet</h2>
              <p>Generate content after cleaning the lead.</p>
            </section>
          )}

          {contentPacket && (
            <section className="card">
              <h2>Preview Website</h2>
              <p className="helper">
                Visual rendering from the generated content packet.
              </p>

              <div className="preview">
                <h1>{contentPacket.headline}</h1>
                <p>{contentPacket.summary}</p>

                <div className="service-grid">
                  {contentPacket.serviceBlocks.map((service, index) => (
                    <div className="service-card" key={index}>
                      <span>0{index + 1}</span>
                      <h3>{service.title}</h3>
                      <p>{service.description}</p>
                    </div>
                  ))}
                </div>

                <button>{contentPacket.CTA}</button>
              </div>

              {previewBuild && (
                <div className="approval-box">
                  <h3>Preview Build Result</h3>
                  <p>
                    <strong>Status:</strong>{" "}
                    {previewBuild.deploymentStatus}
                  </p>
                  <p>
                    <strong>Preview Reference:</strong>{" "}
                    {previewBuild.previewUrl}
                  </p>
                  <p>
                    <strong>Build Reference:</strong>{" "}
                    {previewBuild.buildReference}
                  </p>
                  <p>
  <strong>Review Status:</strong> {reviewStatus}
  {zendeskResult && (
  <div style={{ marginTop: "15px" }}>
    <h4>Zendesk Sync Result</h4>

    <p>
      <strong>Status:</strong>{" "}
      {zendeskResult.syncStatus}
    </p>

    <p>
      <strong>Organization:</strong>{" "}
      {zendeskResult.organizationName}
    </p>

    <p>
      <strong>Ticket ID:</strong>{" "}
      {zendeskResult.zendeskRecordId}
    </p>

    <p>
      <strong>User Email:</strong>{" "}
      {zendeskResult.userEmail}
    </p>

    <a
      href={zendeskResult.ticketUrl}
      target="_blank"
      rel="noreferrer"
    >
      Open Zendesk Ticket
    </a>
  </div>
)}
</p>

<p className="helper">{previewBuild.limitationNote}</p>

<div className="button-row">
  <button onClick={approvePreview}>
    Approve Preview
  </button>

  <button
    className="reject-btn"
    onClick={requestRegeneration}
  >
    Request Regeneration
  </button>
</div>
                </div>
              )}

              <div className="button-row">
                <button onClick={copyContentPacket} className="secondary-btn">
                  Copy Content Packet
                </button>

                <button onClick={downloadPreview}>
                  Download Preview HTML
                </button>

                <button className="reset" onClick={resetApp}>
                  Start New Lead
                </button>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;