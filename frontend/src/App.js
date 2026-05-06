import { useState } from "react";
import axios from "axios";
import "./App.css";

function App() {
  const API_BASE = "http://127.0.0.1:8000";

  const [lead, setLead] = useState({});
  const [cleaned, setCleaned] = useState(null);
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [errors, setErrors] = useState({});
  const [approvalStatus, setApprovalStatus] = useState("Pending Review");

  const handleChange = (e) => {
    setLead({ ...lead, [e.target.name]: e.target.value });
    setMessage("");
    setErrors({});
  };

  const loadSampleLead = () => {
    setLead({
      businessName: "FixIt Plumbing",
      email: "info@fixitplumbing.co.za",
      category: "Plumbing",
      location: "Durban",
      notes:
        "Provides emergency plumbing, pipe repairs, leak detection, and residential plumbing services.",
    });

    setCleaned(null);
    setContent(null);
    setErrors({});
    setApprovalStatus("Pending Review");
    setMessage("Sample lead loaded successfully.");
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

    setMessage("Lead is valid and ready for cleaning.");
    return true;
  };

  const cleanLead = async () => {
    if (!validateLead()) return;

    try {
      setMessage("Cleaning lead data...");

      const response = await axios.post(`${API_BASE}/api/leads/clean`, {
        businessName: lead.businessName,
        email: lead.email,
        category: lead.category,
        location: lead.location,
        notes: lead.notes,
      });

      setCleaned(response.data);
      setContent(null);
      setApprovalStatus("Pending Review");
      setMessage("Lead cleaned successfully.");
    } catch (error) {
      console.error(error);
      setMessage("Backend cleaning failed. Make sure FastAPI is running on port 8000.");
    }
  };

  const generateContent = async () => {
    if (!cleaned) {
      setMessage("Clean the lead before generating content.");
      return;
    }

    try {
      setLoading(true);
      setMessage("Generating AI content packet...");

      const response = await axios.post(
        `${API_BASE}/api/content/generate`,
        cleaned
      );

      setContent(response.data);
      setApprovalStatus("Pending Review");
      setMessage("Preview website generated successfully.");
    } catch (error) {
      console.error(error);
      setMessage("Content generation failed. Check that the backend is running.");
    } finally {
      setLoading(false);
    }
  };

  const copyContentPacket = () => {
    if (!content) return;

    navigator.clipboard.writeText(JSON.stringify(content, null, 2));
    setMessage("Content packet copied to clipboard.");
  };

  const downloadPreview = () => {
    if (!content) return;

    const html = `
<!DOCTYPE html>
<html>
<head>
  <title>${content.headline}</title>
</head>
<body>
  <h1>${content.headline}</h1>
  <p>${content.summary}</p>

  <h2>Services</h2>
  <ul>
    ${content.services
      .map(
        (service) =>
          `<li><strong>${service.title}</strong>: ${service.description}</li>`
      )
      .join("")}
  </ul>

  <p><strong>${content.cta}</strong></p>
</body>
</html>
`;

    const blob = new Blob([html], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");

    link.href = url;
    link.download = "preview-website.html";
    link.click();

    URL.revokeObjectURL(url);
  };

  const resetApp = () => {
    setLead({});
    setCleaned(null);
    setContent(null);
    setLoading(false);
    setMessage("");
    setErrors({});
    setApprovalStatus("Pending Review");
  };

  return (
    <div className="app">
      <header className="header">
        <span className="badge">Phase 1 Frontend + Backend</span>
        <h1>AI Site Factory</h1>
        <p>
          Lead intake, data cleaning, backend content generation, and preview
          website workflow.
        </p>
      </header>

      {message && <div className="message">{message}</div>}

      <div className="layout">
        <div className="left-panel">
          <div className="progress">
            <div className={!cleaned ? "step active" : "step done"}>
              1. Lead
            </div>

            <div
              className={
                cleaned && !content
                  ? "step active"
                  : cleaned
                  ? "step done"
                  : "step"
              }
            >
              2. Clean
            </div>

            <div
              className={loading ? "step active" : content ? "step done" : "step"}
            >
              3. Generate
            </div>

            <div className={content ? "step done" : "step"}>4. Preview</div>
          </div>

          <section className="card">
            <h2>Lead Intake</h2>
            <p className="helper">Enter raw business lead details below.</p>

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

              <button onClick={cleanLead}>Clean Data</button>
            </div>
          </section>
        </div>

        <div className="right-panel">
          {cleaned ? (
            <section className="card">
              <h2>Cleaned Data</h2>
              <p className="helper">
                Normalized lead record returned from the FastAPI backend.
              </p>

              <pre>{JSON.stringify(cleaned, null, 2)}</pre>

              <button onClick={generateContent} disabled={loading}>
                {loading ? "Generating..." : "Generate Content"}
              </button>

              {loading && (
                <div className="loading-box">
                  <div className="spinner"></div>
                  <p>Backend is creating the content packet...</p>
                </div>
              )}
            </section>
          ) : (
            <section className="card empty-state">
              <h2>Cleaned Data</h2>
              <p>Complete lead intake and clean the data to continue.</p>
            </section>
          )}

          {content ? (
            <section className="card">
              <h2>Generated Content Packet</h2>
              <p className="helper">
                Structured content packet returned from the backend generation
                endpoint.
              </p>

              <pre>{JSON.stringify(content, null, 2)}</pre>

              <h2>Preview Website</h2>
              <p className="helper">
                Reviewable website preview generated from the content packet.
              </p>

              <div className="preview">
                <h1>{content.headline}</h1>
                <p>{content.summary}</p>

                <div className="service-grid">
                  {content.services.map((service, index) => (
                    <div className="service-card" key={index}>
                      <span>0{index + 1}</span>
                      <h3>{service.title}</h3>
                      <p>{service.description}</p>
                    </div>
                  ))}
                </div>

                <button>{content.cta}</button>
              </div>

              <div className="approval-box">
                <h3>Approval Status</h3>
                <p>
                  Current Status: <strong>{approvalStatus}</strong>
                </p>

                <div className="button-row">
                  <button onClick={() => setApprovalStatus("Approved")}>
                    Approve Preview
                  </button>

                  <button
                    className="reject-btn"
                    onClick={() =>
                      setApprovalStatus("Rejected - Regenerate Required")
                    }
                  >
                    Reject / Regenerate
                  </button>
                </div>
              </div>

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
          ) : (
            <section className="card empty-state">
              <h2>Preview Website</h2>
              <p>Generate content to view the preview website.</p>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;