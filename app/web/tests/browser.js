/* Browser regression tests: actual DOM, fetch races, real media/image decoding.
   Test-only synthetic inputs; no application internals are exposed or patched. */
const iframe = document.getElementById("preview");
const results = document.getElementById("results");
const summary = document.getElementById("summary");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const assert = (condition, message) => { if (!condition) throw new Error(message); };
async function until(predicate, message) {
  const deadline = Date.now() + 5000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message);
    await delay(10);
  }
}
let w, d, html, fixture, video;
const el = id => d.getElementById(id);
const visible = id => !el(id).classList.contains("hidden");
const click = id => el(id).click();
const response = (data, status = 200) => Promise.resolve(new Response(JSON.stringify(data), {
  status, headers: {"Content-Type": "application/json"}
}));
const done = () => ({job_id:"test-job", status:"done", verdict:"no_violation", violations:[]});
const positive = () => ({...structuredClone(fixture), job_id:"test-job"});

async function fresh() {
  iframe.srcdoc = html.replace('<script src="app.js"></script>',
    '<script src="/tests/setup.js"></script><script src="/app.js"></script>')
    .replace('href="styles.css"', 'href="/styles.css"');
  await new Promise(resolve => iframe.onload = resolve);
  w = iframe.contentWindow; d = w.document;
  el("apiKeyInput").value = "synthetic-test-key";
  el("apiKeyInput").dispatchEvent(new w.Event("input"));
}
function select(name = "synthetic.mov", bytes = video) {
  const transfer = new w.DataTransfer();
  transfer.items.add(new w.File([bytes], name, {type:"video/quicktime"}));
  el("fileInput").files = transfer.files;
  el("fileInput").dispatchEvent(new w.Event("change"));
}
function mode(enabled) {
  el("demoModeToggle").checked = enabled;
  el("demoModeToggle").dispatchEvent(new w.Event("change"));
}
function api(payload, status = 200) {
  w.testFetch = url => url === "/api/v1/analyze"
    ? response({job_id:"test-job"}, status) : response(payload);
}
async function errorResult(payload) {
  api(payload); select(); click("startAnalysisBtn");
  await until(() => visible("errorAlert"), "Malformed response did not fail");
  assert(!visible("resultSection"), "Malformed result was shown");
}

document.getElementById("run").onclick = async function () {
  this.disabled = true; results.textContent = ""; summary.textContent = "Выполняется…";
  let passed = 0, failed = 0;
  async function test(name, fn) {
    const row = document.createElement("li"); results.appendChild(row);
    try {
      await fresh(); await fn();
      assert(w.testErrors.length === 0, w.testErrors.join("; "));
      row.textContent = `PASS — ${name}`; passed++;
    } catch (error) { row.textContent = `FAIL — ${name}: ${error.message}`; failed++; }
    summary.textContent = `${passed} PASS / ${failed} FAIL`;
  }
  try {
    [html, fixture, video] = await Promise.all([
      fetch("/").then(r => r.text()), fetch("/__fixture.json").then(r => r.json()),
      fetch("/__synthetic.mov").then(r => r.blob())
    ]);
    await test("CSS/JS через HTTP; реальный режим по умолчанию", async () => {
      assert(!el("demoModeToggle").checked && !visible("demoBanner"), "Demo is enabled");
      assert(w.getComputedStyle(el("resultSection")).display === "none", "CSS not loaded");
      for (const path of ["/styles.css", "/app.js", "/index.html"]) {
        const r = await fetch(path); assert(r.ok, `${path}: ${r.status}`);
        assert((await r.text()).length > 100, "Empty asset");
      }
      assert(w.testCalls.length === 0, "Unexpected request on startup");
    });
    await test("Успех: события, JPEG, ссылки, модальное окно и переход к таймкоду", async () => {
      const payload = positive();
      payload.violations = [payload.violations[0]];
      payload.violations[0].description = '<img src=x onerror=alert(1)> https://example.invalid/rule';
      payload.violations[0].timecode_sec = 1;
      payload.violations[0].evidence_frames[0].timestamp_sec = 1;
      payload.violations[0].evidence_frames = [payload.violations[0].evidence_frames[0]];
      const second = structuredClone(payload.violations[0]); second.timecode_sec = 3;
      second.evidence_frames = [1,2,3].map(() => ({...second.evidence_frames[0], timestamp_sec:3}));
      payload.violations.push(second);
      api(payload); select();
      await until(() => el("localVideo").readyState >= 1, "Video metadata not decoded");
      click("startAnalysisBtn");
      await until(() => visible("resultSection"), "No positive result");
      assert(el("resultEventsCount").textContent === "2", "Wrong event count");
      assert(d.querySelectorAll(".frame-card__img").length === 4, "Wrong frame count");
      assert(d.querySelector(".event-card__description").children.length === 1, "Unsafe HTML insertion");
      assert(d.querySelector(".event-card__description a").protocol === "https:", "Wrong link");
      d.querySelector(".event-card__top button").click();
      await until(() => Math.abs(el("localVideo").currentTime - 1) < .1, "Seek failed");
      d.querySelector(".frame-card__btn").click(); assert(visible("imageModal"), "No modal");
      click("closeModalBtn"); assert(!visible("imageModal"), "Modal did not close");
      assert(w.testCalls[0].options.headers["X-API-Key"] === "synthetic-test-key", "Missing key");
      assert(w.testCalls[0].options.body.get("file").name === "synthetic.mov", "Wrong multipart field");
    });
    await test("Отрицательный результат и повторная загрузка", async () => {
      api(done()); select(); click("startAnalysisBtn");
      await until(() => visible("resultSection"), "No negative result");
      assert(el("resultHeading").textContent === "Нарушений не выявлено", "Wrong verdict");
      assert(el("resultEventsCount").textContent === "0" && !visible("violationsContainer"), "Events remain");
      click("newUploadBtn");
      assert(!visible("selectedFilePanel") && !visible("videoPanel"), "Old selection remains");
      assert(w.testRevoked.length === 1, "URL not revoked");
      select("second.mov"); click("startAnalysisBtn");
      await until(() => visible("resultSection"), "Second upload failed");
      assert(el("resultFileName").textContent === "second.mov", "Old file name remains");
    });
    await test("failed не становится no_violation", async () => {
      await errorResult({job_id:"test-job", status:"failed"});
      assert(el("errorMessage").textContent.includes("не означает отсутствие"), "Missing explanation");
    });
    for (const code of [401,404,413,415,422,503,500]) {
      await test(`HTTP ${code}`, async () => {
        api(done(), code); select(); click("startAnalysisBtn");
        await until(() => visible("errorAlert"), "No HTTP error");
        assert(el("errorCodeBadge").textContent === `HTTP ${code}`, "Wrong error code");
        assert(!visible("resultSection") && !el("startAnalysisBtn").disabled, "Cannot retry");
      });
    }
    await test("Сетевая ошибка и некорректный JSON", async () => {
      w.testFetch = () => Promise.reject(new TypeError("synthetic offline"));
      select(); click("startAnalysisBtn");
      await until(() => visible("errorAlert"), "Network failure not shown");
      w.testFetch = () => Promise.resolve(new Response("not-json"));
      click("startAnalysisBtn");
      await until(() => visible("errorAlert"), "Invalid JSON not shown");
      assert(!visible("resultSection"), "Invented result");
    });
    await test("Некорректный новый файл очищает предыдущий выбор и URL", async () => {
      select(); const url = el("localVideo").src;
      select("invalid.txt");
      assert(!visible("selectedFilePanel") && !visible("videoPanel"), "Old file still selected");
      assert(w.testRevoked.includes(url), "Old URL remains");
      click("startAnalysisBtn"); assert(w.testCalls.length === 0, "Old file sent");
      select("empty.mov", ""); assert(!visible("selectedFilePanel"), "Empty file accepted");
      select("large.mov", new Blob([new Uint8Array(100 * 1024 * 1024 + 1)]));
      assert(!visible("selectedFilePanel"), "Oversized file accepted");
    });
    await test("Отмена POST: AbortSignal и игнорирование позднего ответа", async () => {
      let resolve;
      w.testFetch = () => new Promise(r => resolve = r); select(); click("startAnalysisBtn");
      const signal = w.testCalls[0].options.signal;
      assert(visible("processingSection"), "Upload cannot be stopped");
      click("cancelPollingBtn"); assert(signal.aborted, "POST not aborted");
      assert(visible("notice") && el("notice").textContent.includes("обработка продолжится"), "No server caveat");
      api(done()); select("new.mov"); click("startAnalysisBtn");
      await until(() => visible("resultSection"), "New job failed");
      resolve(new Response(JSON.stringify({job_id:"stale-job"}))); await delay(100);
      assert(visible("resultSection") && el("resultFileName").textContent === "new.mov", "Stale POST changed screen");
    });
    await test("Отмена GET: поздний JSON не заменяет новое задание с тем же ID", async () => {
      let resolveBody;
      w.testFetch = url => url === "/api/v1/analyze" ? response({job_id:"test-job"}) :
        Promise.resolve({ok:true, json:() => new Promise(resolve => resolveBody = resolve)});
      select(); click("startAnalysisBtn");
      await until(() => !!resolveBody, "GET did not start");
      const signal = w.testCalls[1].options.signal;
      click("cancelPollingBtn"); assert(signal.aborted, "GET not aborted");
      api(done()); select("new.mov"); click("startAnalysisBtn");
      await until(() => visible("resultSection"), "New result missing");
      resolveBody(positive()); await delay(100);
      assert(el("resultEventsCount").textContent === "0", "Stale GET replaced result");
    });
    await test("Реальный POST → деморежим: поздняя ошибка не меняет синтетический результат", async () => {
      let reject;
      w.testFetch = () => new Promise((_, r) => reject = r); select(); click("startAnalysisBtn");
      const signal = w.testCalls[0].options.signal;
      mode(true); assert(signal.aborted && visible("uploadSection"), "Mode did not reset POST");
      el("demoScenarioSelect").value = "no_violation"; click("startAnalysisBtn");
      await until(() => visible("resultSection"), "Demo missing");
      reject(new Error("late network failure")); await delay(100);
      assert(visible("demoBanner") && !visible("errorAlert") && visible("resultSection"), "Late error mixed modes");
      assert(w.testCalls.length === 1, "Demo called real API");
      mode(false); assert(!visible("resultSection") && !visible("demoBanner"), "Synthetic result remains in real mode");
    });
    await test("Реальный GET → демо: поздний ответ не появляется", async () => {
      let resolve;
      w.testFetch = url => url === "/api/v1/analyze" ? response({job_id:"test-job"}) : new Promise(r => resolve = r);
      select(); click("startAnalysisBtn"); await until(() => !!resolve, "GET missing");
      const signal = w.testCalls[1].options.signal; mode(true);
      resolve(new Response(JSON.stringify(positive()))); await delay(100);
      assert(signal.aborted && visible("uploadSection") && !visible("resultSection"), "Late GET mixed modes");
    });
    await test("Демо → реальный режим во время ожидания; фиксация сценария", async () => {
      mode(true); select(); el("demoScenarioSelect").value = "violation"; click("startAnalysisBtn");
      assert(el("demoScenarioSelect").disabled, "Scenario mutable during run");
      mode(false); api(done()); click("startAnalysisBtn");
      await until(() => visible("resultSection"), "Real result missing"); await delay(100);
      assert(!visible("demoBanner") && el("resultJobId").textContent === "test-job", "Demo timer mixed modes");
    });
    await test("Демо без API-ключа: 2 события, 4 JPEG; отмена демо", async () => {
      click("clearKeyBtn"); mode(true); select(); click("startAnalysisBtn");
      await until(() => visible("resultSection"), "Demo requires real key");
      assert(el("resultEventsCount").textContent === "2", "Wrong demo count");
      mode(false); mode(true); click("startAnalysisBtn"); click("cancelPollingBtn"); await delay(100);
      assert(!visible("resultSection") && w.testCalls.length === 0, "Demo cancellation failed");
    });
    const invalid = [
      ["null событие", p => p.violations[0] = null],
      ["строковый таймкод", p => p.violations[0].timecode_sec = "1"],
      ["отрицательный таймкод", p => p.violations[0].timecode_sec = -1],
      ["null таймкод", p => p.violations[0].timecode_sec = null],
      ["пустой тип", p => p.violations[0].type = " "],
      ["описание не строка", p => p.violations[0].description = {}],
      ["нет кадров", p => p.violations[0].evidence_frames = []],
      ["более трёх кадров", p => p.violations[0].evidence_frames = Array(4).fill(p.violations[0].evidence_frames[0])],
      ["null кадр", p => p.violations[0].evidence_frames = [null]],
      ["bool таймкод кадра", p => p.violations[0].evidence_frames[0].timestamp_sec = false],
      ["data URI вместо base64", p => p.violations[0].evidence_frames[0].image_base64 = "data:image/jpeg;base64,AA=="],
      ["повреждённый JPEG с сигнатурами", p => p.violations[0].evidence_frames[0].image_base64 = "/9j/2Q=="],
      ["лишнее поле", p => p.violations[0].extra = true],
      ["другой job_id", p => p.job_id = "someone-else"],
      ["неупорядоченные события", p => p.violations.push({...p.violations[0],timecode_sec:0})],
      ["несогласованный verdict", p => p.verdict = "no_violation"]
    ];
    for (const [name, mutate] of invalid) {
      await test(`Валидация: ${name}`, async () => { const p = positive(); mutate(p); await errorResult(p); });
    }
    await test("Таймкод вне длительности доступного видео", async () => {
      select(); await until(() => el("localVideo").readyState >= 1, "No duration");
      const p = positive(); p.violations[0].timecode_sec = 100;
      api(p); click("startAnalysisBtn");
      await until(() => visible("errorAlert"), "Out of range time accepted");
      assert(!visible("resultSection"), "Out of range shown");
    });
    await test("processing/failed с посторонними итоговыми полями отклоняются", async () => {
      await errorResult({job_id:"test-job",status:"processing",verdict:null});
    });
    await test("Ошибка кодека/декодирования не блокирует отправку", async () => {
      api(done()); select("unsupported.mov", "synthetic invalid media");
      await until(() => el("videoMessage").textContent.includes("кодек"), "No codec message");
      click("startAnalysisBtn"); await until(() => visible("resultSection"), "Analysis blocked by browser codec");
    });
    await test("Освобождение object URL при замене и уходе со страницы", async () => {
      select(); select("replacement.mov");
      assert(w.testCreated.length === 2 && w.testRevoked.length === 1, "Replacement leaked URL");
      w.dispatchEvent(new w.PageTransitionEvent("pagehide"));
      assert(w.testRevoked.length === 2 && el("apiKeyInput").value === "", "Pagehide cleanup failed");
    });
    // Leave a useful synthetic result visible for manual visual inspection.
    await fresh(); mode(true); select(); click("startAnalysisBtn");
    await until(() => visible("resultSection"), "Final preview missing");
    summary.textContent = `Завершено: ${passed} PASS / ${failed} FAIL. Только синтетические данные.`;
  } catch (error) { summary.textContent = `Harness error: ${error.message}`; }
  this.disabled = false;
};
