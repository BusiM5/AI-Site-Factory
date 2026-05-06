import { useState } from "react";
import "./App.css";

function App() {
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
  };

  const loadSampleLead = () => {
  setLead({
    businessName: "FixIt Plumbing",
    email: "info@fixitplumbing.co.za",
    category: "Plumbing",
    location: "Durban",
    notes: "Provides emergency plumbing, pipe repairs, leak detection, and residential plumbing services.",
  });

  setCleaned(null);
  setContent(null);
  setErrors({});
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

  const cleanLead = () => {
    if (!validateLead()) return;

    const cleanedData = {
      businessName: lead.businessName.trim(),
      email: lead.email.trim().toLowerCase(),
      category: lead.category.trim(),
      location: lead.location?.trim() || "Not provided",
      cleanSummary: lead.notes?.trim() || "No additional notes provided.",
      status: "CLEAN",
      readyForAI: "YES",
    };

    setCleaned(cleanedData);
    setContent(null);
    setMessage("Lead cleaned successfully.");
  };

  const generateContent = () => {
    if (!cleaned) {
      setMessage("Clean the lead before generating content.");
      return;
    }

    setLoading(true);
    setMessage("Generating AI content packet...");

    setTimeout(() => {
      const generated = {
        headline: `${cleaned.businessName} - ${cleaned.category} Services in ${cleaned.location}`,
        summary: `${cleaned.businessName} provides reliable ${cleaned.category.toLowerCase()} services in ${cleaned.location}. ${cleaned.cleanSummary}`,
        services: [
          `Professional ${cleaned.category} support`,
          "Customer-focused service delivery",
          "Reliable local assistance",
        ],
        cta: `Contact ${cleaned.businessName} today to learn more.`,
      };

      setContent(generated);
      setLoading(false);
      setMessage("Preview website generated successfully.");
    }, 2000);
  };

  const resetApp = () => {
    setLead({});
    setCleaned(null);
    setContent(null);
    setLoading(false);
    setMessage("");
    setApprovalStatus("Pending Review");
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
    ${content.services.map((service) => `<li>${service}</li>`).join("")}
  </ul>
  <strong>${content.cta}</strong>
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

  return (
    <div className="app">
      <header className="header">
        <span className="badge">Phase 1 Frontend</span>
        <h1>AI Site Factory</h1>
        <p>
          Lead intake, data cleaning, content generation, and preview website
          workflow.
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
                cleaned && !content ? "step active" : cleaned ? "step done" : "step"
              }
            >
              2. Clean
            </div>
            <div className={loading ? "step active" : content ? "step done" : "step"}>
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
            {errors.businessName && <p className="error">{errors.businessName}</p>}

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
                Normalized lead record ready for AI generation.
              </p>

              <pre>{JSON.stringify(cleaned, null, 2)}</pre>

              <button onClick={generateContent} disabled={loading}>
                {loading ? "Generating..." : "Generate Content"}
              </button>

              {loading && (
                <div className="loading-box">
                  <div className="spinner"></div>
                  <p>AI is creating the content packet...</p>
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
                      <p>{service}</p>
                    </div>
                  ))}
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
                  onClick={() => setApprovalStatus("Rejected - Regenerate Required")}
                 >
                    Reject / Regenerate
                 </button>
              </div>
                </div> 

                <button>{content.cta}</button>
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