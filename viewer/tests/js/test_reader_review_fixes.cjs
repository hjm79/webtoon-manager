const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createWriter } = require("../../static/reader-progress.js");
const html = fs.readFileSync(path.join(__dirname, "../../static/reader.html"), "utf8");
const session = "00000000-0000-4000-8000-000000000001";
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("ordinary saves serialize delayed responses and capture the latest backward-reading intent", async () => {
  const requests = []; let finish;
  const writer = createWriter({ clientSessionId: session, send: async (body) => {
    requests.push(body);
    if (requests.length === 1) await new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => ({ saved: true }) };
  } });
  const older = writer.write({ chapter_id: "last", page_index: 9, completed: true });
  await tick();
  const newer = writer.write({ chapter_id: "earlier", page_index: 0, page_offset: 0.4, completed: false });
  await tick();
  assert.equal(requests.length, 1);
  finish();
  assert.equal((await older).latest, false);
  const latest = await newer;
  assert.equal(latest.latest, true);
  assert.equal(requests[1].chapter_id, "earlier");
  assert.equal(requests[1].page_offset, 0.4);
  assert.deepEqual(requests.map((r) => r.client_sequence), [1, 2]);
  assert.ok(requests.every((r) => r.client_session_id === session));
});

test("pagehide keepalive dispatches final intent while an older request is unresolved", async () => {
  const requests = []; let finish;
  const writer = createWriter({ clientSessionId: session, send: async (body, options) => {
    requests.push({ body, options });
    if (body.client_sequence === 1) await new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => ({ saved: body.client_sequence > 1 }) };
  } });
  const older = writer.write({ chapter_id: "a", page_index: 8 });
  await tick();
  const final = await writer.write({ chapter_id: "b", page_index: 1 }, { keepalive: true, urgent: true });
  assert.equal(requests.length, 2);
  assert.equal(requests[1].options.keepalive, true);
  assert.equal(final.saved, true);
  finish();
  const stale = await older;
  assert.equal(stale.saved, false);
  assert.equal(stale.latest, false);
});

test("a rejected save does not poison the next serialized retry", async () => {
  let calls = 0;
  const writer = createWriter({ clientSessionId: session, send: async () => {
    if (++calls === 1) throw new Error("offline");
    return { ok: true, json: async () => ({ saved: true }) };
  } });
  assert.equal((await writer.write({ page_index: 1 })).ok, false);
  assert.equal((await writer.write({ page_index: 2 })).ok, true);
});

function clock() {
  let now = 0, sequence = 0;
  const timers = new Map();
  return {
    schedule(callback, delay) { const id = ++sequence; timers.set(id, { callback, at: now + delay }); return id; },
    cancel(id) { timers.delete(id); },
    advance(ms) {
      const end = now + ms;
      while (true) {
        const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at; timers.delete(due[0]); due[1].callback();
      }
      now = end;
    },
  };
}

test("continuous position changes save by five seconds and retain a trailing save", () => {
  const time = clock(); let saves = 0;
  const writer = createWriter({ clientSessionId: session, send() {}, schedule: time.schedule, cancel: time.cancel });
  for (let index = 0; index < 20; index++) {
    writer.scheduleSave(() => saves++);
    time.advance(500);
  }
  assert.equal(saves, 2);
  writer.scheduleSave(() => saves++);
  time.advance(1199); assert.equal(saves, 2);
  time.advance(1); assert.equal(saves, 3);
});

function element(tag = "div") {
  const classes = new Set();
  const el = {
    tagName: tag.toUpperCase(), children: [], dataset: {}, style: {}, attrs: {}, isConnected: true,
    classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name), toggle() {} },
    setAttribute(name, value) { this.attrs[name] = value; },
    append(...children) { children.forEach((child) => this.appendChild(child)); },
    appendChild(child) { child.parent = this; this.children.push(child); },
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); },
    addEventListener(name, callback) { (this.listeners ||= {})[name] = callback; },
    querySelector(selector) { return this.children.find((child) => selector === ".reader-chapter-error" && child.className === "reader-chapter-error") || null; },
    scrollIntoView() {}, focus() {},
  };
  return el;
}

function chapters(fetcher) {
  const pages = element(); const original = element("img"); pages.appendChild(original);
  const c = { pagesEl: pages, isLoadingNext: false, loadedChapters: [{ id: "one" }],
    document: { createElement: element }, loadMoreObserver: { disconnect() {}, observe() {} },
    fetch: fetcher, encodeURIComponent, stopAutoScroll() {}, revealPages() {}, postReaderEvent() {}, readingTimeMessage: () => "지금은 읽을 수 있는 시간이 아닙니다.",
    createPageImage: () => ({ img: element("img"), wrap: element() }), updateReaderPosition() {},
    chapterIndexById: () => 1, seriesData: { chapters: [{ id: "one" }, { id: "two" }] },
    appendEndCard() {}, positionAtImage: async () => {}, prefetchNextChapter() {},
  };
  vm.createContext(c);
  vm.runInContext(html.match(/function showChapterLoadError\([^]*?(?=\nasync function loadNextChapter)/)[0], c);
  return { c, pages, original };
}

test("next chapter 503 preserves existing pages and an inline retry appends the recovered chapter", async () => {
  let calls = 0;
  const f = chapters(async () => ++calls === 1
    ? { ok: false, status: 503, json: async () => ({}) }
    : { ok: true, json: async () => ({ page_count: 2 }) });
  assert.equal(await f.c.appendChapter({ id: "two" }), false);
  assert.equal(f.pages.children[0], f.original);
  assert.equal(f.c.loadedChapters.length, 1);
  const notice = f.pages.children[1];
  await notice.children[1].listeners.click();
  assert.equal(f.pages.children[0], f.original);
  assert.equal(f.pages.children.some((node) => node.className === "reader-chapter-error"), false);
  assert.equal(f.c.loadedChapters.length, 2);
  assert.equal(f.c.isLoadingNext, false);
});

test("a network error leaves the current chapter available and offers retry", async () => {
  const f = chapters(async () => { throw new Error("offline"); });
  assert.equal(await f.c.appendChapter({ id: "two" }), false);
  assert.equal(f.pages.children[0], f.original);
  assert.equal(f.pages.children[1].children[1].textContent, "회차 다시 불러오기");
});

test("time restricted next chapter stays blocked without discarding the current chapter", async () => {
  const f = chapters(async () => ({ ok: false, status: 403, json: async () => ({ detail: { reason: "outside_allowed_reading_time" } }) }));
  await f.c.appendChapter({ id: "two" });
  assert.equal(f.pages.children[0], f.original);
  assert.match(f.pages.children[1].children[0].textContent, /읽을 수 있는 시간/);
  assert.equal(f.c.loadedChapters.length, 1);
});

function scrolling() {
  const auto = element("button"), sidebar = element(), frames = new Map(); let id = 0;
  const c = { autoScrollActive: false, autoScrollFrame: null, autoScrollToggleEl: auto,
    document: { getElementById: () => sidebar, documentElement: { scrollHeight: 10000 } },
    window: { scrollY: 0, innerHeight: 800, scrollBy(_x, y) { this.scrollY += y; } },
    performance: { now: () => 0 }, SCROLL_PIXELS_PER_SECOND: 450, scrollSpeed: 1,
    stopContinuousScroll() {}, requestAnimationFrame: (callback) => { frames.set(++id, callback); return id; },
    cancelAnimationFrame: (key) => frames.delete(key),
  };
  vm.createContext(c);
  vm.runInContext(html.match(/function updateAutoScrollButton\([^]*?(?=\n\/\/ 화면 한 칸)/)[0], c);
  vm.runInContext(html.match(/function pauseReaderScrolling\([^]*?\n\}/)[0], c);
  vm.runInContext(html.match(/function pauseForManualPointer\([^]*?\n\}/)[0], c);
  return { c, auto, sidebar, frames };
}

test("explicit autoscroll button reports running state and pause cancels its frame", () => {
  const f = scrolling();
  f.auto.listeners.click();
  assert.equal(f.auto.attrs["aria-pressed"], "true");
  assert.equal(f.auto.attrs["aria-label"], "자동 스크롤 정지");
  assert.equal(f.frames.size, 1);
  f.c.pauseReaderScrolling();
  assert.equal(f.c.autoScrollActive, false);
  assert.equal(f.auto.attrs["aria-pressed"], "false");
  assert.equal(f.frames.size, 0);
});

test("touching the stop button does not pause before its click and accidentally restart", () => {
  const f = scrolling(); f.c.startAutoScroll();
  f.c.pauseForManualPointer({ target: { closest: () => f.auto } });
  assert.equal(f.c.autoScrollActive, true);
  f.auto.listeners.click();
  assert.equal(f.c.autoScrollActive, false);
  f.c.startAutoScroll();
  f.c.pauseForManualPointer({ target: { closest: () => null } });
  assert.equal(f.c.autoScrollActive, false);
});

test("Space on the speed control keeps native activation and body Space toggles autoscroll", () => {
  let toggles = 0, prevented = 0;
  const c = { document: { getElementById: () => ({ classList: { contains: () => false } }) }, toggleAutoScroll: () => toggles++ };
  vm.createContext(c);
  vm.runInContext(html.match(/function handleScrollKeyDown\([^]*?(?=\nfunction handleScrollKeyUp)/)[0], c);
  c.handleScrollKeyDown({ target: { tagName: "BUTTON" }, key: " ", code: "Space", preventDefault: () => prevented++ });
  assert.equal(toggles, 0); assert.equal(prevented, 0);
  c.handleScrollKeyDown({ target: { tagName: "BODY" }, key: " ", code: "Space", preventDefault: () => prevented++ });
  assert.equal(toggles, 1); assert.equal(prevented, 1);
});

test("sidebar open pauses scrolling, focuses current chapter and Escape restores the trigger", () => {
  const sidebar = element(), overlay = element(), trigger = element("button"), close = element("button");
  const rows = [element("button"), element("button")]; let paused = 0;
  const nodes = { sidebar, sidebarOverlay: overlay, sidebarToggle: trigger, sidebarClose: close };
  const doc = { activeElement: trigger, getElementById: (key) => nodes[key] };
  [trigger, close, ...rows].forEach((node) => { node.focus = () => { doc.activeElement = node; }; });
  sidebar.querySelectorAll = () => [close, ...rows];
  const c = { document: doc, sidebarReturnFocus: null, pauseReaderScrolling: () => paused++,
    sidebarListEl: { querySelector: () => rows[1], querySelectorAll: () => rows } };
  vm.createContext(c);
  vm.runInContext(html.match(/function openSidebar\([^]*?(?=\ndocument.getElementById\("sidebar"\).addEventListener)/)[0], c);
  c.openSidebar();
  assert.equal(paused, 1); assert.equal(sidebar.inert, false); assert.equal(doc.activeElement, rows[1]);
  let prevented = 0;
  c.handleSidebarKeyDown({ key: "Home", preventDefault: () => prevented++ });
  assert.equal(doc.activeElement, rows[0]);
  c.handleSidebarKeyDown({ key: "ArrowDown", preventDefault: () => prevented++ });
  assert.equal(doc.activeElement, rows[1]);
  c.handleSidebarKeyDown({ key: "Tab", preventDefault: () => prevented++ });
  assert.equal(doc.activeElement, close);
  c.handleSidebarKeyDown({ key: "Escape", preventDefault: () => prevented++ });
  assert.equal(doc.activeElement, trigger); assert.equal(sidebar.inert, true);
  assert.equal(trigger.attrs["aria-expanded"], "false");
  assert.equal(prevented, 4);
});

test("chapter entries use native buttons so Enter and Space activate chapter navigation", () => {
  const list = element(); const selected = [];
  const c = { seriesData: { chapters: [{ id: "one", label: "1화" }] }, sidebarListEl: list,
    document: { createElement: element }, jumpToChapter: (ch) => selected.push(ch.id), highlightSidebar() {} };
  vm.createContext(c);
  vm.runInContext(html.match(/function renderSidebar\([^]*?(?=\nfunction highlightSidebar)/)[0], c);
  c.renderSidebar();
  assert.equal(list.children[0].tagName, "BUTTON");
  assert.equal(list.children[0].type, "button");
  list.children[0].listeners.click();
  assert.deepEqual(selected, ["one"]);
});


test("only an actually opened chapter requests a trusted reading grant", async () => {
  const requests = [];
  const f = chapters(async (url) => { requests.push(url); return { ok: true, json: async () => ({ page_count: 1 }) }; });
  await f.c.appendChapter({ id: "two" });
  assert.equal(requests[0], "/api/chapters/two/pages?dimensions=1&start_reading=true");
  const prefetch = html.match(/async function prefetchNextChapter\([^]*?(?=\nasync function revealSkippedPages)/)[0];
  assert.equal(prefetch.includes("start_reading"), false);
});
