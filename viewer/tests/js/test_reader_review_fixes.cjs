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
    replaceChildren(...children) { this.children = []; this.append(...children); },
    append(...children) { children.forEach((child) => this.appendChild(child)); },
    appendChild(child) { child.parent = this; this.children.push(child); },
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); },
    addEventListener(name, callback) { (this.listeners ||= {})[name] = callback; },
    querySelector(selector) { return this.children.find((child) => selector === `.${child.className}`) || null; },
    scrollIntoView() {}, focus() {},
  };
  return el;
}

function chapters(fetcher) {
  const pages = element(); const original = element("img"); pages.appendChild(original);
  const events = [];
  const c = { pagesEl: pages, isLoadingNext: false, IS_PROFILE_VIEW: false, loadedChapters: [{ id: "one" }],
    currentChapterId: "one", currentPageIndex: 3,
    document: { createElement: element }, loadMoreObserver: { disconnect() {}, observe() {} },
    fetch: fetcher, encodeURIComponent, stopAutoScroll() {}, revealPages() {}, postReaderEvent: (name) => events.push(name),
    readingTimeMessage: () => "지금은 읽을 수 있는 시간이 아닙니다.",
    createPageImage: () => ({ img: element("img"), wrap: element() }), updateReaderPosition() {},
    chapterIndexById: () => 1, seriesData: { chapters: [{ id: "one" }, { id: "two" }] },
    appendEndCard() {}, positionAtImage: async () => {}, prefetchNextChapter() {},
  };
  vm.createContext(c);
  vm.runInContext(html.match(/function showChapterLoadError\([^]*?(?=\nasync function loadNextChapter)/)[0], c);
  return { c, pages, original, events };
}

test("next chapter 503 preserves existing pages and an inline retry appends the recovered chapter", async () => {
  let calls = 0;
  let finish;
  const f = chapters(async () => ++calls === 1
    ? { ok: false, status: 503, json: async () => ({}) }
    : new Promise((resolve) => { finish = resolve; }));
  assert.equal(await f.c.appendChapter({ id: "two" }), false);
  assert.equal(f.pages.children[0], f.original);
  assert.equal(f.c.loadedChapters.length, 1);
  const notice = f.pages.children[1];
  const retrying = notice.children[1].listeners.click();
  assert.equal(f.pages.children[1], notice);
  assert.equal(notice.attrs.role, "status");
  assert.match(notice.children[0].textContent, /다시 불러오는 중/);
  finish({ ok: true, json: async () => ({ page_count: 2 }) });
  await retrying;
  assert.equal(f.pages.children[0], f.original);
  assert.equal(f.pages.children.some((node) => node.className === "reader-chapter-error"), false);
  assert.equal(f.c.loadedChapters.length, 2);
  assert.equal(f.c.isLoadingNext, false);
  assert.deepEqual(f.events, ["error", "ready"]);
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
  const compactStop = element("button");
  const c = { autoScrollActive: false, autoScrollFrame: null, autoScrollToggleEl: auto,
    stopHiddenAutoScrollEl: compactStop,
    visiblePages: new Map(), isLoadingNext: false, pagesEl: element(),
    document: { getElementById: () => sidebar, documentElement: { scrollHeight: 10000 } },
    window: { scrollY: 0, innerHeight: 800, scrollBy(_x, y) { this.scrollY += y; } },
    performance: { now: () => 0 }, SCROLL_PIXELS_PER_SECOND: 450, scrollSpeed: 1,
    stopContinuousScroll() {}, noteReadingInput() {}, requestAnimationFrame: (callback) => { frames.set(++id, callback); return id; },
    cancelAnimationFrame: (key) => frames.delete(key),
  };
  vm.createContext(c);
  vm.runInContext(html.match(/function updateAutoScrollButton\([^]*?(?=\n\/\/ 화면 한 칸)/)[0], c);
  vm.runInContext(html.match(/function pauseReaderScrolling\([^]*?\n\}/)[0], c);
  vm.runInContext(html.match(/function pauseForManualPointer\([^]*?\n\}/)[0], c);
  return { c, auto, sidebar, frames, compactStop, advance(now) {
    const [key, step] = frames.entries().next().value;
    frames.delete(key);
    step(now);
  } };
}

test("explicit autoscroll button reports running state and pause cancels its frame", () => {
  const f = scrolling();
  f.auto.listeners.click();
  assert.equal(f.auto.attrs["aria-pressed"], "true");
  assert.equal(f.auto.attrs["aria-label"], "자동 스크롤 정지");
  assert.equal(f.compactStop.hidden, false);
  assert.equal(f.frames.size, 1);
  f.c.pauseReaderScrolling();
  assert.equal(f.c.autoScrollActive, false);
  assert.equal(f.auto.attrs["aria-pressed"], "false");
  assert.equal(f.compactStop.hidden, true);
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

test("automatic scrolling waits at a pending chapter boundary and resumes when content is ready", () => {
  const f = scrolling();
  const boundary = element(); boundary.className = "reader-next-boundary";
  f.c.pagesEl.appendChild(boundary);
  f.c.window.scrollY = 9200;
  f.c.startAutoScroll();
  f.advance(16);
  assert.equal(f.c.autoScrollActive, true);
  assert.equal(f.c.window.scrollY, 9200);
  f.c.isLoadingNext = true;
  f.advance(32);
  assert.equal(f.c.autoScrollActive, true);
  boundary.remove();
  f.c.isLoadingNext = false;
  f.c.document.documentElement.scrollHeight = 12000;
  f.advance(48);
  assert.ok(f.c.window.scrollY > 9200);
  f.c.window.scrollY = 11200;
  f.advance(64);
  assert.equal(f.c.autoScrollActive, false);
});

test("an offscreen prefetch failure pauses automatic scrolling only when the panel reaches the viewport", () => {
  const f = scrolling();
  let top = 850;
  f.c.visiblePages.set("cut", {
    dataset: { imageState: "failed" }, isConnected: true,
    getBoundingClientRect: () => ({ top, bottom: top + 220 }),
  });
  f.c.startAutoScroll();
  f.advance(16);
  assert.equal(f.c.autoScrollActive, true);
  top = 785;
  f.advance(32);
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

test("sidebar opens at the sorted beginning and preserves native search and sort controls", () => {
  const sidebar = element(), overlay = element(), trigger = element("button"), close = element("button");
  const search = Object.assign(element("input"), { value: "" });
  const order = element("select");
  const rows = [element("button"), element("button")]; let paused = 0;
  const nodes = { sidebar, sidebarOverlay: overlay, sidebarToggle: trigger, sidebarClose: close, sidebarSearch: search };
  const doc = { activeElement: trigger, getElementById: (key) => nodes[key] };
  [trigger, close, search, order, ...rows].forEach((node) => { node.focus = () => { doc.activeElement = node; }; });
  sidebar.querySelectorAll = () => [close, search, order, ...rows];
  const c = { document: doc, sidebarReturnFocus: null, pauseReaderScrolling: () => paused++,
    sidebarEpisodeOrderEl: order, sidebarListEl: { scrollTop: 50, querySelector: () => rows[1], querySelectorAll: () => rows } };
  vm.createContext(c);
  vm.runInContext(html.match(/function filterSidebarChapters\([^]*?(?=\ndocument.getElementById\("sidebar"\).addEventListener)/)[0], c);
  c.openSidebar();
  assert.equal(paused, 1); assert.equal(sidebar.inert, false); assert.equal(doc.activeElement, close);
  assert.equal(c.sidebarListEl.scrollTop, 0);
  let prevented = 0;
  search.focus();
  c.handleSidebarKeyDown({ key: "Home", preventDefault: () => prevented++ });
  assert.equal(doc.activeElement, search);
  order.focus();
  c.handleSidebarKeyDown({ key: "ArrowDown", preventDefault: () => prevented++ });
  assert.equal(doc.activeElement, order);
  rows[1].focus();
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

test("chapter list sorts display rows while preserving source indices and native activation", () => {
  const list = element(); const selected = [];
  const label = "1화 <b>제목</b>";
  const chapters = [{ id: "one", label }, { id: "two", label: "2화" }];
  const c = { seriesData: { chapters }, sidebarListEl: list, sidebarEpisodeOrderEl: { value: "latest" }, filterSidebarChapters() {},
    document: { createElement: element }, jumpToChapter: (ch) => selected.push(ch.id), highlightSidebar() {} };
  vm.createContext(c);
  vm.runInContext(html.match(/function renderSidebar\([^]*?(?=\nfunction highlightSidebar)/)[0], c);
  c.renderSidebar();
  assert.deepEqual(list.children.map((row) => row.dataset.idx), ["1", "0"]);
  assert.deepEqual(chapters.map((chapter) => chapter.id), ["one", "two"]);
  assert.equal(list.children[1].tagName, "BUTTON");
  assert.equal(list.children[1].type, "button");
  assert.equal(list.children[1].children[0].textContent, label);
  assert.equal(list.children[1].innerHTML, undefined);
  list.children[0].listeners.click();
  assert.deepEqual(selected, ["two"]);
  c.sidebarEpisodeOrderEl.value = "oldest";
  c.renderSidebar();
  assert.deepEqual(list.children.map((row) => row.dataset.idx), ["0", "1"]);
});

test("chapter order persists and follows updates from the manager's shared browser storage", () => {
  const order = Object.assign(element("select"), { value: "latest" });
  const events = element();
  const writes = []; const displayed = [];
  const c = { window: events, localStorage: { getItem: () => "oldest", setItem: (...args) => writes.push(args) },
    sidebarEpisodeOrderEl: order, sidebarListEl: { scrollTop: 60 }, seriesData: {}, renderSidebar: () => displayed.push(order.value) };
  vm.createContext(c);
  vm.runInContext(html.match(/const EPISODE_ORDER_KEY[^]*?(?=\ndocument.getElementById\("clearSidebarSearch"\).addEventListener)/)[0], c);
  assert.equal(order.value, "oldest");
  order.value = "latest";
  order.listeners.change();
  assert.deepEqual(writes, [["webtoonEpisodeOrder", "latest"]]);
  events.listeners.storage({ key: "webtoonEpisodeOrder", newValue: "oldest" });
  events.listeners.storage({ key: null, newValue: null });
  assert.deepEqual(displayed, ["latest", "oldest", "latest"]);
  assert.equal(c.sidebarListEl.scrollTop, 0);
});

test("chapter search distinguishes 1화 from 12화, matches titles, and returns to the current chapter", () => {
  const rows = [element("button"), element("button")];
  rows.forEach((row, index) => { row.dataset.idx = index; });
  let focused, scrolled;
  rows[1].focus = () => { focused = rows[1]; };
  rows[1].scrollIntoView = ({ block }) => { scrolled = block; };
  const search = Object.assign(element("input"), { value: "" });
  const clear = element("button"), empty = element();
  const nodes = { sidebarSearch: search, clearSidebarSearch: clear, sidebarSearchEmpty: empty };
  const c = { document: { getElementById: (key) => nodes[key] },
    sidebarListEl: { querySelectorAll: () => rows, querySelector: () => rows[1] },
    seriesData: { chapters: [{ label: "01화 시작" }, { label: "12화 바람이 분다" }] },
  };
  vm.createContext(c);
  vm.runInContext(html.match(/function filterSidebarChapters\([^]*?(?=\nfunction openSidebar\()/)[0], c);
  search.value = "０１ 화";
  c.filterSidebarChapters();
  assert.deepEqual(rows.map((row) => row.hidden), [false, true]);
  search.value = "바람";
  c.filterSidebarChapters();
  assert.deepEqual(rows.map((row) => row.hidden), [true, false]);
  search.value = "없는 화";
  c.filterSidebarChapters();
  assert.equal(empty.hidden, false);
  c.focusCurrentSidebarChapter();
  assert.deepEqual(rows.map((row) => row.hidden), [false, false]);
  assert.equal(empty.hidden, true);
  assert.equal(clear.disabled, true);
  assert.equal(focused, rows[1]);
  assert.equal(scrolled, "center");
});

test("toolbar hiding removes focus access and keeps an accessible reveal control", () => {
  const topbar = element(), zoomBar = element(), readerQuickTools = element();
  const autoScrollToggleEl = element("button"), showReaderTools = element("button");
  let focused;
  const document = { get activeElement() { return focused; } };
  const focusable = (keyboard) => ({ matches: () => keyboard });
  const keyboardFocus = focusable(true), pointerFocus = focusable(false);
  topbar.contains = (node) => [keyboardFocus, pointerFocus].includes(node);
  zoomBar.contains = () => false;
  readerQuickTools.contains = (node) => node === showReaderTools;
  showReaderTools.focus = () => { focused = showReaderTools; };
  autoScrollToggleEl.focus = () => { focused = autoScrollToggleEl; };
  const c = { document, topbar, zoomBar, readerQuickTools, showReaderTools, autoScrollToggleEl, isAdjustingZoom: false };
  vm.createContext(c);
  vm.runInContext(html.match(/function setChromeHidden\([^]*?\n\}/)[0], c);
  c.setChromeHidden(false);
  focused = keyboardFocus;
  c.setChromeHidden(true);
  assert.equal(topbar.inert, false);
  focused = pointerFocus;
  c.setChromeHidden(true);
  assert.equal(topbar.inert, true);
  assert.equal(zoomBar.inert, true);
  assert.equal(readerQuickTools.hidden, false);
  assert.equal(focused, showReaderTools);
  c.setChromeHidden(false);
  assert.equal(readerQuickTools.hidden, true);
  assert.equal(focused, autoScrollToggleEl);
});

test("chapter buttons respect the ends and display the rendered page count", () => {
  const previousChapterEl = element("button"), nextChapterEl = element("button"), chapterProgressEl = element("progress");
  const chapters = [{ id: "first" }, { id: "last" }], selections = [];
  const c = { seriesData: { chapters }, currentChapterId: "first", currentPageIndex: 0,
    loadedChapters: [{ id: "first", pageCount: 40 }], readerPositionEl: element(), chapterProgressEl,
    previousChapterEl, nextChapterEl, chapterIndexById: (id) => chapters.findIndex((item) => item.id === id),
    jumpToChapter: (chapter) => selections.push(chapter.id) };
  vm.createContext(c);
  vm.runInContext(html.match(/function updateReaderPosition\([^]*?(?=\nfunction updateCurrentFromVisible\()/)[0], c);
  c.updateReaderPosition();
  assert.equal(previousChapterEl.disabled, true);
  assert.equal(nextChapterEl.disabled, false);
  assert.equal(chapterProgressEl.max, 40);
  assert.equal(chapterProgressEl.value, 1);
  previousChapterEl.listeners.click();
  nextChapterEl.listeners.click();
  assert.deepEqual(selections, ["last"]);
  c.currentChapterId = "last";
  c.updateReaderPosition();
  assert.equal(previousChapterEl.disabled, false);
  assert.equal(nextChapterEl.disabled, true);
});


test("only an actually opened chapter requests a trusted reading grant", async () => {
  const requests = [];
  const f = chapters(async (url) => { requests.push(url); return { ok: true, json: async () => ({ page_count: 1 }) }; });
  await f.c.appendChapter({ id: "two" });
  assert.equal(requests[0], "/api/chapters/two/pages?dimensions=1&start_reading=true");
  const prefetch = html.match(/async function prefetchNextChapter\([^]*?(?=\nasync function revealSkippedPages)/)[0];
  assert.equal(prefetch.includes("start_reading"), false);
});

function chapterBoundary(fetcher, shared = false) {
  const fixture = chapters(fetcher);
  const imageRequests = [];
  Object.assign(fixture.c, {
    IS_PROFILE_VIEW: shared, prefetchedChapterIds: new Set(), prefetchedChapterOverlap: null,
    chapterIndexById: (id) => fixture.c.seriesData.chapters.findIndex((chapter) => chapter.id === id),
    withProfilePrefix: (url) => url,
    prefetchImages: (urls) => imageRequests.push(...urls),
  });
  vm.runInContext(html.match(/async function prefetchNextChapter\([^]*?(?=\nasync function revealSkippedPages)/)[0], fixture.c);
  vm.runInContext(html.match(/async function loadNextChapter\([^]*?(?=\nfunction appendEndCard)/)[0], fixture.c);
  return { ...fixture, imageRequests };
}

test("next chapter opens its manifest while waiting on the prefetched recap result", async () => {
  const urls = [];
  let releaseOverlap;
  const overlap = new Promise((resolve) => { releaseOverlap = resolve; });
  const f = chapterBoundary(async (url) => {
    urls.push(url);
    return url.endsWith("/overlap") ? overlap : { ok: true, json: async () => ({ page_count: 3 }) };
  });
  const warming = f.c.prefetchNextChapter("one");
  const opening = f.c.loadNextChapter();
  await tick();
  assert.deepEqual(f.imageRequests, ["/api/chapters/two/pages/0"]);
  assert.deepEqual(urls, ["/api/chapters/two/overlap", "/api/chapters/two/pages?dimensions=1&start_reading=true"]);
  assert.equal(f.c.loadedChapters.length, 1);
  releaseOverlap({ ok: true, json: async () => ({ skip_pages: 1 }) });
  await Promise.all([warming, opening]);
  assert.equal(f.c.loadedChapters.length, 2);
  assert.equal(f.pages.children[1].className, "overlap-reveal-wrap");
  assert.equal(f.pages.children.length, 4);
});

test("changing chapters while a shared profile awaits recap does not grant or append the old chapter", async () => {
  const urls = [];
  let releaseOverlap;
  const f = chapterBoundary(async (url) => {
    urls.push(url);
    return new Promise((resolve) => { releaseOverlap = resolve; });
  }, true);
  const staleOpening = f.c.loadNextChapter();
  await tick();
  f.c.loadedChapters = [{ id: "replacement" }];
  f.c.isLoadingNext = true;
  releaseOverlap({ ok: true, json: async () => ({ skip_pages: 0 }) });
  await staleOpening;
  assert.deepEqual(urls, ["/api/chapters/two/overlap"]);
  assert.equal(f.c.isLoadingNext, true);
  assert.deepEqual(f.pages.children, [f.original]);
});

test("a shared profile does not replace the current grant until the decoded chapter's boundary reaches its viewport", async () => {
  const urls = [];
  const f = chapterBoundary(async (url) => {
    urls.push(url);
    return { ok: true, json: async () => url.endsWith("/overlap") ? { skip_pages: 0 } : { page_count: 1 } };
  }, true);
  f.c.loadedChapters = [];
  f.c.window = { innerHeight: 800 };
  let emit, observed, config;
  f.c.IntersectionObserver = class {
    constructor(callback, options) { emit = callback; config = options; }
    disconnect() {}
    observe(target) { observed = target; }
  };
  vm.runInContext(html.match(/function reachedProfileBoundary\([^]*?(?=\nconst pageObserver)/)[0]
    + "\nthis.loadMoreObserver = loadMoreObserver;", f.c);
  await f.c.appendChapter({ id: "one" });
  assert.equal(config.rootMargin, "0px");
  assert.equal(observed.className, "reader-next-boundary");
  let top = 850;
  observed.getBoundingClientRect = () => ({ top, bottom: top + 2 });
  const notify = () => emit([{ target: observed, isIntersecting: true }]);
  f.c.loadedChapters[0].lastImg.dataset.imageState = "loading";
  top = 799;
  notify();
  f.c.loadedChapters[0].lastImg.dataset.imageState = "loaded";
  top = 850;
  notify();
  await tick();
  const manifest = "/api/chapters/two/pages?dimensions=1&start_reading=true";
  assert.equal(urls.includes(manifest), false);
  top = 799;
  notify();
  await tick();
  assert.equal(urls.filter((url) => url === manifest).length, 1);
  assert.equal(f.c.loadedChapters.length, 2);
});

test("reader starts independent requests together and waits for preferences before restoring position", async () => {
  const started = [];
  const ready = new Map();
  function blocked(name) {
    started.push(name);
    return new Promise((resolve) => ready.set(name, resolve));
  }
  const chapter = { id: "two", label: "2화" };
  let restored = null;
  const c = {
    seriesId: "series", initialChapterId: null, initialPage: 0, initialOffset: 0, seriesData: null,
    fullscreenPreference: false, readingFromChapterId: null, currentChapterId: null, pendingJumpChapterId: null, currentPageIndex: 0,
    currentPageOffset: 0, chapterTitleEl: {}, pagesEl: { querySelector: () => true },
    document: { getElementById: () => ({}) }, window: { fetch() {} },
    initializeEmbed: () => blocked("embed"),
    fetch: () => blocked("chapters"),
    WebtoonReaderPosition: { resolve: () => blocked("position") },
    loadReaderSettingsScope: () => blocked("scope"),
    loadScrollSpeed: () => blocked("speed"), readFullscreenPreference: () => blocked("fullscreen"),
    loadZoomPercent: () => blocked("zoom"), applyZoom: () => started.push("applyZoom"),
    updateFullscreenButton() {}, renderSidebar() {}, highlightSidebar() {}, beginPositioning() {}, updateCurrentFromVisible() {}, recordVisibleReading() {},
    showError: () => assert.fail("unexpected reader startup error"),
    chapterById: (id) => id === chapter.id ? chapter : null,
    appendChapter: (item, { scrollToPage, pageOffset }) => { restored = [item.id, scrollToPage, pageOffset]; },
    postReaderEvent: (event) => started.push(event),
  };
  vm.createContext(c);
  vm.runInContext(html.match(/async function load\(\) \{[^]*?(?=\nload\(\)\.catch)/)[0], c);
  const loading = c.load();
  assert.deepEqual(started, ["embed"]);
  ready.get("embed")(true);
  await tick();
  assert.deepEqual(started, ["embed", "position", "chapters", "scope"]);
  ready.get("scope")();
  await tick();
  assert.deepEqual(started.slice(4), ["speed", "fullscreen", "zoom"]);
  ready.get("position")({ chapterId: "two", pageIndex: 4, pageOffset: 0.37 });
  ready.get("chapters")({ ok: true, json: async () => ({ title: "webtoon", chapters: [chapter] }) });
  ready.get("speed")();
  ready.get("fullscreen")();
  await tick();
  assert.equal(restored, null);
  ready.get("zoom")();
  await loading;
  assert.deepEqual(restored, ["two", 4, 0.37]);
  assert.deepEqual(started.slice(-2), ["applyZoom", "ready"]);
});
