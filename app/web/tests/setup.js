// Loaded only by browser.html, never by the production index.
window.testCalls = [];
window.testRevoked = [];
window.testCreated = [];
window.testErrors = [];
window.addEventListener("error", e => window.testErrors.push(e.message));
window.addEventListener("unhandledrejection", e => window.testErrors.push(String(e.reason)));
const realTimeout = window.setTimeout.bind(window);
window.setTimeout = (fn, ms, ...args) => realTimeout(fn, ms === 2000 ? 20 : ms, ...args);
const realCreate = URL.createObjectURL.bind(URL);
const realRevoke = URL.revokeObjectURL.bind(URL);
URL.createObjectURL = blob => { const url = realCreate(blob); testCreated.push(url); return url; };
URL.revokeObjectURL = url => { testRevoked.push(url); realRevoke(url); };
window.fetch = (url, options) => {
  testCalls.push({url, options});
  if (!window.testFetch) throw new Error("Unexpected test request");
  return window.testFetch(url, options);
};
