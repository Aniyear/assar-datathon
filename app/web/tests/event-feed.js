// Additional integration regressions; original browser.js remains unchanged.
const frame = document.getElementById('preview');
const summary = document.getElementById('summary');
const assert = (ok, message) => { if (!ok) throw new Error(message); };
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check) {
  const end = Date.now() + 7000;
  while (!check()) { if (Date.now() > end) throw new Error('UI timeout'); await delay(20); }
}
document.getElementById('run').onclick = async function () {
  this.disabled = true;
  document.getElementById('results').textContent = '';
  let pass = 0, fail = 0;
  const [html, fixture, movie] = await Promise.all([
    fetch('/').then(r => r.text()), fetch('/__fixture.json').then(r => r.json()), fetch('/__synthetic.mov').then(r => r.blob())
  ]);
  for (const width of [1440, 1024, 390]) {
    const row = document.createElement('li'); document.getElementById('results').append(row);
    try {
      frame.style.width = width + 'px';
      frame.srcdoc = html.replace(/<script src="app\.js([^\"]*)"><\/script>/,
        '<script src="/tests/setup.js"></script><script src="/app.js$1"></script>').replace('href="styles.css', 'href="/styles.css');
      await new Promise(resolve => frame.onload = resolve);
      const w = frame.contentWindow, d = w.document, el = id => d.getElementById(id);
      const payload = { ...structuredClone(fixture), job_id: 'test-feed' };
      payload.violations = Array.from({length:20}, (_, i) => {
        const event = structuredClone(fixture.violations[0]);
        event.timecode_sec = .2 + i * .2;
        event.description = `SYNTHETIC EVENT ${i + 1}. ` + (i === 4 ? 'Long synthetic description. '.repeat(120) : 'Test description. ') + 'https://example.invalid/rule';
        event.evidence_frames = Array.from({length:i % 3 + 1}, () => ({...event.evidence_frames[0], timestamp_sec:event.timecode_sec}));
        return event;
      });
      w.testFetch = url => Promise.resolve(new Response(JSON.stringify(url === '/api/v1/analyze' ? {job_id:'test-feed'} : payload), {headers:{'Content-Type':'application/json'}}));
      el('apiKeyInput').value = 'synthetic-test-key'; el('apiKeyInput').dispatchEvent(new w.Event('input'));
      const transfer = new w.DataTransfer(); transfer.items.add(new w.File([movie], 'synthetic.mov', {type:'video/quicktime'}));
      el('fileInput').files = transfer.files; el('fileInput').dispatchEvent(new w.Event('change'));
      await until(() => el('localVideo').readyState >= 1);
      el('startAnalysisBtn').click();
      await until(() => !el('resultSection').classList.contains('hidden'));
      await delay(200);
      const chips = [...el('eventTimeline').children];
      assert(chips.length === 20, 'Events lost');
      assert(el('previousEventBtn').disabled && !el('nextEventBtn').disabled, 'First boundary');
      w.scrollTo(0, el('eventNavigator').getBoundingClientRect().top + w.scrollY - 20);
      await delay(100);
      const before = {y:w.scrollY, height:d.documentElement.scrollHeight};
      chips[4].focus({preventScroll:true}); chips[4].click();
      await until(() => Math.abs(el('localVideo').currentTime - 1) < .05);
      assert(el('selectedEventStatus').textContent === 'Событие 5 из 20', 'Wrong selected event');
      assert(d.querySelectorAll('.event-card:not([hidden])').length === 1, 'Multiple open cards');
      const card = el('event-card-5');
      assert(card.scrollHeight > card.clientHeight, 'Long description is not scrollable');
      assert(card.textContent.includes(payload.violations[4].description.split('https:')[0]), 'Description lost');
      assert(card.querySelector('a').href === 'https://example.invalid/rule', 'Source lost');
      assert(w.scrollY === before.y && d.documentElement.scrollHeight === before.height, 'Page jumped on event 5');
      assert(d.activeElement === chips[4], 'Focus moved from selector');
      card.querySelector('.event-card__top button').click(); await delay(80);
      assert(w.scrollY === before.y, 'Timecode scrolled page');
      el('nextEventBtn').click();
      assert(el('selectedEventStatus').textContent === 'Событие 6 из 20', 'Next failed');
      el('previousEventBtn').click();
      assert(el('selectedEventStatus').textContent === 'Событие 5 из 20', 'Previous failed');
      chips[19].click(); await delay(80);
      assert(el('nextEventBtn').disabled && !el('previousEventBtn').disabled, 'Last boundary');
      assert(el('eventTimeline').scrollLeft > 0, 'Last chip not revealed');
      assert(w.scrollY === before.y && d.documentElement.scrollHeight === before.height, 'Page jumped on last event');
      assert(d.documentElement.scrollWidth <= w.innerWidth + 1, 'Page horizontal overflow');
      if (width >= 1024) {
        const video = el('videoPanel').getBoundingClientRect(), detail = el('violationsContainer').getBoundingClientRect();
        assert(video.right < detail.left && Math.abs(video.top-detail.top) < 2, 'Player is not beside event');
      }
      el('newUploadBtn').click();
      assert(el('eventNavigator').classList.contains('hidden') && !el('violationsList').children.length, 'Old events remain');
      assert(w.testErrors.length === 0, w.testErrors.join('; '));
      row.textContent = `PASS — ${width}px: 20 events, event 5, seek, fixed page height/scroll, full source, boundaries, reset`; pass++;
    } catch (error) { row.textContent = `FAIL — ${width}px: ${error.message}`; fail++; }
    summary.textContent = `${pass} PASS / ${fail} FAIL`;
  }
  this.disabled = false;
};
