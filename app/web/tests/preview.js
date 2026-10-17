// Review harness only. The production index never imports this file.
const view = new URLSearchParams(location.search).get("view") || "upload";
const preview = document.getElementById("designPreview");
for (const link of document.querySelectorAll("nav a")) {
  if (link.search === "?view=" + view) link.setAttribute("aria-current", "page");
}
document.getElementById("size").addEventListener("change", event => {
  preview.classList.toggle("mobile", event.target.value === "mobile");
});
preview.addEventListener("load", () => {
  const doc = preview.contentDocument;
  doc.getElementById("demoModeToggle").click();
  // Real API calls are forbidden in this explicitly labelled visual preview.
  preview.contentWindow.fetch = () => Promise.reject(new Error("API disabled in design preview"));
  if (view !== "upload") {
    doc.getElementById("demoScenarioSelect").value = view === "clean" ? "no_violation" : view === "failed" ? "failed" : "violation";
    doc.getElementById("useDemoSampleBtn").click();
    doc.getElementById("startAnalysisBtn").click();
  }
});
