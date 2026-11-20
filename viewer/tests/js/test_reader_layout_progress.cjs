const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createReadingEvidence } = require("../../static/reader-progress.js");

const html = fs.readFileSync(path.join(__dirname, "../../static/reader.html"), "utf8");
const finish = html.match(/function confirmLastChapterAtDocumentEnd\([\s\S]*?(?=\nwindow.addEventListener\("scroll", confirmLastChapterAtDocumentEnd)/)[0];
const update = html.match(/function updateCurrentFromVisible\([\s\S]*?(?=\nfunction onPositionChange)/)[0];

function image(page, { loaded = true, top = 0, bottom = 500, height = bottom - top } = {}) {
  return {
    dataset: { chapter: "only", page: String(page) },
    complete: loaded,
    naturalWidth: loaded ? 600 : 0,
    naturalHeight: loaded ? 1000 : 0,
    getBoundingClientRect: () => ({ top, bottom, height }),
  };
}

function reader(images, { docHeight = 900, scrollY = 0, positioning = false, visible = images, scoped = false } = {}) {
  let saves = 0;
  let updates = 0;
  const context = {
    endOfDocumentCheckQueued: false,
    requestAnimationFrame: (callback) => callback(),
    seriesData: { title: "실제 함수 검증", chapters: [{ id: "only" }] },
    currentChapterId: "only",
    pendingJumpChapterId: null,
    readingFromChapterId: scoped ? "only" : null,
    activeProgressScope: (id) => scoped ? id : null,
    chapterIndexById: (id) => id === "only" ? 0 : 1,
    readingEvidence: createReadingEvidence(), completedChapters: new Set(),
    progressRevision: 1, hasReadingInput: true,
    currentPageIndex: 0,
    chapterTitleEl: { textContent: "" },
    document: { documentElement: { scrollHeight: docHeight } },
    window: { scrollY, innerHeight: 900 },
    pagesEl: { querySelector: () => images[images.length - 1], querySelectorAll: () => images, classList: { contains: () => positioning } },
    CSS: { escape: (value) => value },
    chapterById: () => null,
    highlightSidebar() {},
    updateUrlState() {},
    saveProgressNow: () => { saves++; },
    onPositionChange: () => { updates++; },
    visiblePages: new Map(visible.map((img, i) => [i, img])),
    loadedChapters: [{ id: "only", lastImg: images.at(-1), pageCount: Number(images.at(-1)?.dataset.page) + 1 }],
  };
  vm.createContext(context);
  vm.runInContext(`${finish}\n${update}`, context);
  return { context, saves: () => saves, updates: () => updates };
}

test("temporary short document cannot mark unloaded last page as chapter completed", () => {
  const ctx = reader([image(0), image(39, { loaded: false, top: 500, bottom: 500, height: 0 })], { docHeight: 500 });
  ctx.context.confirmLastChapterAtDocumentEnd();
  assert.equal(ctx.context.currentPageIndex, 0);
  assert.equal(ctx.saves(), 0);
});

test("last image loading first cannot complete while preceding image layout is missing", () => {
  const ctx = reader([image(0, { loaded: false, height: 0 }), image(39, { top: 400, bottom: 800 })]);
  ctx.context.confirmLastChapterAtDocumentEnd();
  assert.equal(ctx.context.currentPageIndex, 0);
  assert.equal(ctx.saves(), 0);
});

test("last image must actually reach the viewport even when document metrics say bottom", () => {
  const ctx = reader([image(0, { top: 0, bottom: 500 }), image(39, { top: 1400, bottom: 2000 })]);
  ctx.context.confirmLastChapterAtDocumentEnd();
  assert.equal(ctx.saves(), 0);
});

test("a decoded and traversed final page at the real bottom records completion", () => {
  const first = image(0, { top: 0, bottom: 500 });
  const last = image(1, { top: 500, bottom: 1240 });
  const ctx = reader([first, last]);
  ctx.context.updateCurrentFromVisible();
  first.getBoundingClientRect = () => ({ top: -500, bottom: 0, height: 500 });
  last.getBoundingClientRect = () => ({ top: 0, bottom: 740, height: 740 });
  ctx.context.confirmLastChapterAtDocumentEnd();
  assert.equal(ctx.context.currentPageIndex, 1);
  assert.equal(ctx.saves(), 1);
});

test("restore-position overlay blocks completion and observer progress until positioned", () => {
  const ctx = reader([image(39, { top: 0, bottom: 740 })], { positioning: true });
  ctx.context.confirmLastChapterAtDocumentEnd();
  ctx.context.updateCurrentFromVisible();
  assert.equal(ctx.context.currentPageIndex, 0);
  assert.equal(ctx.saves(), 0);
  assert.equal(ctx.updates(), 0);
});

test("unloaded or zero-height observer entries cannot replace a real visible reading page", () => {
  const ctx = reader([image(4, { top: 0, bottom: 800 }), image(39, { loaded: false, top: -1, bottom: -1, height: 0 })]);
  ctx.context.updateCurrentFromVisible();
  assert.equal(ctx.context.currentPageIndex, 4);
  assert.equal(ctx.updates(), 1);
});

test("out-of-order last image cannot advance progress past preceding pages that still have no layout", () => {
  const ctx = reader([image(0, { loaded: false, top: 0, bottom: 0, height: 0 }), image(39, { top: 0, bottom: 740 })]);
  ctx.context.updateCurrentFromVisible();
  assert.equal(ctx.context.currentPageIndex, 0);
  assert.equal(ctx.updates(), 0);
});

test("a decoded visible page still updates while only later pages are pending", () => {
  const ctx = reader([image(4, { top: 0, bottom: 740 }), image(39, { loaded: false, top: 740, bottom: 740, height: 0 })]);
  ctx.context.updateCurrentFromVisible();
  assert.equal(ctx.context.currentPageIndex, 4);
  assert.equal(ctx.updates(), 1);
});

test("large decoded pages still count as current with a small visible fraction", () => {
  const ctx = reader([image(7, { top: -2000, bottom: 3000, height: 5000 }), image(8, { top: 3000, bottom: 4000 })]);
  ctx.context.updateCurrentFromVisible();
  assert.equal(ctx.context.currentPageIndex, 7);
  assert.equal(ctx.updates(), 1);
});

test("scrolling within a page only measures nearby images even after many chapters load", () => {
  let measurements = 0;
  const images = Array.from({ length: 1200 }, (_, i) => {
    const img = image(i, { top: (i - 600) * 1000 - 200, bottom: (i - 599) * 1000 - 200 });
    const rect = img.getBoundingClientRect;
    img.getBoundingClientRect = () => { measurements++; return rect(); };
    return img;
  });
  const ctx = reader(images, { docHeight: 1200000, scoped: true, visible: images.slice(599, 602) });
  ctx.context.seriesData.chapters.push({ id: "next" });
  ctx.context.currentPageIndex = 600;
  ctx.context.updateCurrentFromVisible();
  assert.equal(ctx.context.currentPageIndex, 600);
  assert.equal(ctx.context.currentPageOffset, 0.2);
  assert.ok(measurements < 10, `${measurements} geometry reads`);
});
