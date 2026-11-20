const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { resolve } = require("../../static/reader-position.js");
const { createWriter, createReadingEvidence } = require("../../static/reader-progress.js");
const html = fs.readFileSync(path.join(__dirname, "../../static/reader.html"), "utf8");
const tracker = html.match(/function updateCurrentFromVisible\([^]*?(?=\nfunction onPositionChange)/)[0];
const changed = html.match(/function onPositionChange\([^]*?\n\}/)[0];
const save = html.match(/function progressImageState\([^]*?(?=\nfunction scheduleSaveProgress)/)[0];
const finish = html.match(/function confirmLastChapterAtDocumentEnd\([^]*?(?=\nwindow.addEventListener\("scroll", confirmLastChapterAtDocumentEnd)/)[0];
const url = html.match(/const POSITION_URL_INTERVAL_MS[^]*?(?=\nlet chapterJumpSequence)/)[0];
const jump = html.match(/let chapterJumpSequence[^]*?(?=\nlet sidebarReturnFocus)/)[0];

function image(page, top, height, extra = {}) {
  return { dataset: { chapter: "last", page: String(page), imageState: "loaded" },
    complete: true, naturalWidth: 720, naturalHeight: height,
    getBoundingClientRect: () => ({ top, bottom: top + height, height }), ...extra };
}
function reader(images, { docHeight = 1000, scrollY = 200, readingFromChapterId = "last", fetchResult = () => ({ ok: true }) } = {}) {
  const bodies = []; const timers = []; const moves = [];
  let clock = 0;
  let urlTimer = null;
  let urlWrites = 0;
  const location = { pathname: "/reader", search: "?series=series&chapter=last&page=4&page_offset=0.6&embed=1" };
  const context = { seriesId: "series", seriesData: { title: "Test", chapters: [{ id: "first" }, { id: "last" }] },
    currentChapterId: "last", pendingJumpChapterId: null, readingFromChapterId, completedChapterPosition: null, closingProgressSaved: false,
    readingEvidence: createReadingEvidence(), completedChapters: new Set(), persistedCompletedChapters: new Set(),
    progressRevision: 1, savedProgressRevision: 0, hasReadingInput: true, readingInputUntil: 0,
    currentPageIndex: 0, currentPageOffset: 0, saveTimer: null,
    chapterIndexById: (id) => ["first", "last"].indexOf(id),
    endOfDocumentCheckQueued: false, window: { scrollY, innerHeight: 800 },
    document: { documentElement: { scrollHeight: docHeight }, getElementById: () => ({ classList: { contains: () => false } }) },
    pagesEl: { querySelectorAll: () => images, querySelector: () => null, classList: { contains: () => false } },
    chapterById: () => null, chapterTitleEl: { textContent: "" }, highlightSidebar() {}, updateReaderPosition() {},
    scheduleSaveProgress: () => timers.push(true), setProgressSaveError() {}, postReaderEvent() {},
    clearTimeout() {}, requestAnimationFrame: (callback) => callback(), URLSearchParams, location,
    setTimeout: (fn) => { urlTimer = fn; return 1; }, Date: { now: () => clock }, CSS: { escape: (value) => value },
    history: { replaceState(_state, _title, next) { urlWrites++; location.search = next.slice(next.indexOf("?")); } },
    fetch: async (_url, options) => { bodies.push((({ client_session_id, client_sequence, ...position }) => position)(JSON.parse(options.body))); return fetchResult(); },
    loadedChapters: ["first", "last"].map(id => {
      const chapterImages = images.filter(img => img.dataset.chapter === id);
      const lastImg = chapterImages.at(-1);
      return { id, firstImg: chapterImages[0], lastImg, pageCount: Number(lastImg?.dataset.page) + 1 };
    }),
    visiblePages: new Map(images.map((img, index) => [index, img])),
    cancelPendingAnchor() {}, closeSidebar() {}, pauseReaderScrolling() {}, setChromeHidden() {},
    positionAtImage: async (img, offset) => {
      moves.push({ img, offset });
      for (const page of images) {
        const height = page.getBoundingClientRect().height;
        const top = page === img ? 0 : 2000;
        page.getBoundingClientRect = () => ({ top, bottom: top + height, height });
      }
      return true;
    },
    embedBridge: { active: true },
  };
  context.progressWriter = createWriter({ clientSessionId: "00000000-0000-4000-8000-000000000001",
    send: (body, { keepalive }) => context.fetch(`/api/series/${context.seriesId}/progress`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive,
    }) });
  vm.createContext(context);
  vm.runInContext([tracker, changed, save, finish, url, jump].join("\n"), context);
  return { context, bodies, timers, moves, params: () => new URLSearchParams(location.search),
    flushUrl() { clock += 500; const flush = urlTimer; urlTimer = null; flush?.(); },
    urlWrites: () => urlWrites };
}

test("a failed explicit jump cannot save the end of the previous chapter visible beside it", async () => {
  const oldEnd = image(4, -360, 700);
  oldEnd.dataset.chapter = "first";
  const failed = image(0, 340, 550, { complete: true, naturalWidth: 0, naturalHeight: 0 });
  failed.dataset.imageState = "failed";
  const { context, bodies } = reader([oldEnd, failed], { readingFromChapterId: "last" });
  context.pendingJumpChapterId = "last";
  assert.equal(await context.saveProgressNow({ keepalive: true }), true);
  assert.equal(context.currentChapterId, "last");
  assert.equal(context.pendingJumpChapterId, "last");
  assert.equal(bodies.length, 0);
});

test("a partially read tall final image sends ordinary progress with the precise offset", async () => {
  const f = reader([image(4, -1200, 4000)], { docHeight: 5000, scrollY: 1200 });
  await f.context.saveProgressNow();
  assert.deepEqual(f.bodies, [{ chapter_id: "last", page_index: 4, completed: false, page_offset: 0.3,
    read_chapter_ids: [], reading_from_chapter_id: "last" }]);
  assert.equal(f.params().get("page_offset"), "0.3");
});

test("a short final image stays completed through the following observer and ordinary save", async () => {
  const f = reader([image(0, 0, 500), image(1, 500, 300)]);
  for (let i = 0; i < 20; ++i) f.context.confirmLastChapterAtDocumentEnd();
  f.context.updateCurrentFromVisible();
  await f.context.saveProgressNow();
  assert.equal(f.context.currentPageIndex, 1);
  assert.equal(f.context.currentPageOffset, 0);
  assert.deepEqual(f.bodies, [
    { chapter_id: "last", page_index: 1, completed: true, read_chapter_ids: [], reading_from_chapter_id: "last" },
  ]);
  assert.equal(f.params().get("page"), "1");
  assert.equal(f.params().has("page_offset"), false);
});

test("document end retains the correct offset inside a tall final image", async () => {
  const final = image(0, 0, 4000);
  const f = reader([final]);
  for (let y = 0; y <= 3200; y += 400) {
    final.getBoundingClientRect = () => ({ top: -y, bottom: 4000 - y, height: 4000 });
    f.context.updateCurrentFromVisible();
  }
  await f.context.saveProgressNow();
  assert.deepEqual(f.bodies, [{ chapter_id: "last", page_index: 0, completed: true, page_offset: 0.8,
    read_chapter_ids: [], reading_from_chapter_id: "last" }]);
});

test("a direct jump to a decoded final page saves a cursor without finishing the skipped pages", async () => {
  const f = reader([image(0, -1500, 500), image(1, 0, 740)]);
  await f.context.saveProgressNow();
  assert.deepEqual(f.bodies, [{ chapter_id: "last", page_index: 1, completed: false,
    read_chapter_ids: [], reading_from_chapter_id: "last" }]);
});

test("a visible pending page prevents final completion even with reserved geometry", async () => {
  const pending = image(0, -200, 500, { complete: false, naturalWidth: 0, naturalHeight: 0 });
  const f = reader([pending, image(1, 300, 300)]);
  f.context.confirmLastChapterAtDocumentEnd();
  assert.equal(f.bodies.length, 0);
  assert.equal(f.context.currentPageIndex, 0);
  await f.context.saveProgressNow();
  assert.equal(f.bodies.length, 0);
});

test("chapter navigation clears a stale fractional URL before an explicit-link reload", async () => {
  const first = image(0, -1200, 1000);
  first.dataset.chapter = "first";
  const f = reader([first, image(0, -250, 1000)]);
  await f.context.jumpToChapter({ id: "first", label: "1" });
  f.flushUrl();
  const params = f.params();
  assert.equal(params.get("chapter"), "first");
  assert.equal(params.get("page"), "0");
  assert.equal(params.has("page_offset"), false);
  assert.equal(params.get("embed"), "1");
  assert.equal(f.moves[0].offset, 0);
  assert.deepEqual(await resolve({ seriesId: "series", chapterId: params.get("chapter"),
    pageIndex: Number(params.get("page")), pageOffset: Number(params.get("page_offset")) }),
  { chapterId: "first", pageIndex: 0 });
});

test("shelf progress scopes read tracking to the selected chapter and resets the scope on explicit jumps", async () => {
  const first = image(0, -1200, 1000);
  first.dataset.chapter = "first";
  const f = reader([first, image(1, 0, 1000)], { readingFromChapterId: "last" });
  await f.context.saveProgressNow();
  assert.equal(f.bodies[0].reading_from_chapter_id, "last");
  await f.context.jumpToChapter({ id: "first", label: "1" });
  await f.context.saveProgressNow();
  assert.equal(f.bodies.at(-1).reading_from_chapter_id, "first");
});

test("a chapter can be finished without opening the next one when fully visible", async () => {
  const page = image(0, 170, 450);
  page.dataset.chapter = "first";
  const f = reader([page], { docHeight: 7000, readingFromChapterId: "first" });
  f.context.currentChapterId = "first";
  await f.context.saveProgressNow();
  assert.deepEqual(f.bodies, [{ chapter_id: "first", page_index: 0, completed: true,
    read_chapter_ids: [], reading_from_chapter_id: "first" }]);
  page.getBoundingClientRect = () => ({ top: 400, bottom: 850, height: 450 });
  await f.context.saveProgressNow();
  assert.equal(f.bodies.length, 1);
});

test("a failed upcoming chapter image does not prevent completing the current chapter", async () => {
  const finalPage = image(0, 170, 450);
  finalPage.dataset.chapter = "first";
  const next = image(0, 620, 450, { naturalWidth: 0, naturalHeight: 0 });
  next.dataset.imageState = "failed";
  const f = reader([finalPage, next], { docHeight: 7000, readingFromChapterId: "first" });
  f.context.currentChapterId = "first";
  await f.context.saveProgressNow();
  assert.deepEqual(f.bodies, [{ chapter_id: "first", page_index: 0, completed: true,
    read_chapter_ids: [], reading_from_chapter_id: "first" }]);
});

test("a completion write can be retried after failure and is reused after acknowledgement", async () => {
  let attempts = 0;
  const f = reader([image(0, 0, 300)], { fetchResult: () => ({ ok: ++attempts > 1 }) });
  assert.equal(await f.context.saveProgressNow(), false);
  assert.equal(await f.context.saveProgressNow(), true);
  assert.equal(await f.context.saveProgressNow({ urgent: true }), true);
  assert.equal(attempts, 2);
});

test("scrolling to another page synchronizes its own offset and preserves precise deep links", async () => {
  const f = reader([image(7, -960, 1600)], { docHeight: 5000, scrollY: 1200 });
  f.context.updateCurrentFromVisible();
  const params = f.params();
  assert.equal(params.get("page"), "7");
  assert.equal(params.get("page_offset"), "0.6");
  assert.deepEqual(await resolve({ seriesId: "series", chapterId: params.get("chapter"),
    pageIndex: Number(params.get("page")), pageOffset: Number(params.get("page_offset")) }),
  { chapterId: "last", pageIndex: 7, pageOffset: 0.6 });
  f.context.updateUrlState("last", 0);
  f.flushUrl();
  assert.equal(f.params().has("page_offset"), false);
});

test("rapid scrolling coalesces address changes to the latest precise position", () => {
  const f = reader([]);
  for (let i = 0; i <= 100; ++i) f.context.updateUrlState("last", 8, i / 200);
  assert.equal(f.urlWrites(), 1);
  f.flushUrl();
  assert.equal(f.urlWrites(), 2);
  assert.equal(f.params().get("page_offset"), "0.5");
});

test("a denied address update cannot prevent saving the exact scroll position", async () => {
  const f = reader([image(7, -960, 1600)], { docHeight: 5000, scrollY: 1200 });
  f.context.history.replaceState = () => { throw new Error("History update denied"); };
  f.context.updateCurrentFromVisible();
  assert.equal(f.timers.length, 1);
  assert.equal(await f.context.saveProgressNow(), true);
  assert.equal(f.bodies[0].page_offset, 0.6);
});
