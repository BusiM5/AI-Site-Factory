import { useState } from "react";
import "./App.css";

function App() {
  const [lead, setLead] = useState({});
  const [cleaned, setCleaned] = useState(null);
  const [content, setContent] = useState(null);

  const handleChange = (e) => {
    setLead({ ...lead, [e.target.name]: e.target.value });
  };

  const validateLead = () => {
    if (!lead.businessName || !lead.email || !lead.category) {
      alert("Missing required fields: Business Name, Email, and Category.");
      return;
    }
    alert("Lead is valid and ready for cleaning.");
  };

  const cleanLead = () => {
    const cleanedData = {
      businessName: lead.businessName?.trim(),
      email: lead.email?.trim().toLowerCase(),
      category: lead.category?.trim(),
      location: lead.location?.trim(),
      cleanSummary: lead.notes?.trim(),
      status: "CLEAN",
      readyForAI: "YES",
    };

    setCleaned(cleanedData);
  };

  const generateContent = () => {
    if (!cleaned) return alert("Please clean the lead first.");

    const generated = {
      headline: `${cleaned.businessName} - Professional ${cleaned.category} Services`,
      summary: `${cleaned.businessName} provides reliable ${cleaned.category.toLowerCase()} services in ${cleaned.location}. We focus on simple, professional, and customer-friendly solutions.`,
      services: [
        `Professional ${cleaned.category} support`,
        "Customer-focused service delivery",
        "Reliable local assistance",
      ],
      cta: `Contact ${cleaned.businessName} today to learn more.`,
    };

    setContent(generated);
  };

  return (
    <div className="app">
      <div className="hero">
        <p className="tag">Phase 1 Prototype</p>
        <h1>AI Site Factory</h1>
        <p className="subtitle">
          Lead intake, cleaning, AI content generation, and preview website flow.
        </p>
      </div>

      <div className="workflow">
        <div className="card">
          <div className="step">Step 1</div>
          <h2>Lead Intake</h2>
          <p className="helper">Enter the raw business lead details.</p>

          <input name="businessName" placeholder="Business Name" onChange={handleChange} />
          <input name="email" placeholder="Email Address" onChange={handleChange} />
          <input name="category" placeholder="Category / Industry" onChange={handleChange} />
          <input name="location" placeholder="Location" onChange={handleChange} />
          <textarea name="notes" placeholder="Business Notes" onChange={handleChange}></textarea>

          <div className="button-row">
            <button className="btn secondary" onClick={validateLead}>Validate Lead</button>
            <button className="btn primary" onClick={cleanLead}>Clean Data</button>
          </div>
        </div>

        {cleaned && (
          <div className="card">
            <div className="step">Step 2</div>
            <h2>Cleaned Lead</h2>
            <p className="helper">This is the structured lead ready for AI generation.</p>

            <div className="data-box">
              <pre>{JSON.stringify(cleaned, null, 2)}</pre>
            </div>

            <button className="btn purple" onClick={generateContent}>
              Generate Website Content
            </button>
          </div>
        )}

        {content && (
          <div className="card preview-card">
            <div className="step">Step 3</div>
            <h2>Preview Website</h2>
            <p className="helper">Generated preview based on the cleaned lead data.</p>

            <div className="website-preview">
              <div className="preview-hero">
                <h1>{content.headline}</h1>
                <p>{content.summary}</p>
                <button>{content.cta}</button>
              </div>

              <div className="services">
                <h3>Services</h3>
                <div className="service-grid">
                  {content.services.map((service, index) => (
                    <div className="service-card" key={index}>
                      <span>0{index + 1}</span>
                      <p>{service}</p>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default App;