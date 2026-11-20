const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../../static/reader.html"), "utf8");
const jump = html.match(/async function jumpToChapter\([^]*?\n\}/)[0];
const boundary = html.match(/async function moveToChapterBoundary\([^]*?\n\}/)[0];

function reader({ chapters, saveProgress, shownPageZero = null } = {}) {
  const events = [];
  const context = {
    chapterJumpSequence: 0, currentChapterId: "old", pendingJumpChapterId: null, currentPageIndex: 4, currentPageOffset: 0.25,
    readingFromChapterId: "old", completedChapterPosition: null, closingProgressSaved: false,
    progressRevision: 0, readingInputUntil: 0, hasReadingInput: false,
    seriesData: { title: "Series" }, loadedChapters: chapters || [], isLoadingNext: false,
    chapterTitleEl: { textContent: "" },
    document: { getElementById: () => ({ classList: { contains: () => false } }) },
    CSS: { escape: (id) => id }, window: { innerHeight: 800 },
    pagesEl: { querySelector: () => shownPageZero, replaceChildren() {} },
    pageObserver: { disconnect() {} }, loadMoreObserver: { disconnect() {} },
    imageLoader: { destroy() {} }, newImageLoader: () => ({}), visiblePages: new Map(),
    pauseReaderScrolling() {}, stopContinuousScroll() {}, stopAutoScroll() {}, beginPositioning() {},
    cancelPendingAnchor() {}, highlightSidebar() {}, updateReaderPosition() {}, recordVisibleReading() {},
    updateCurrentFromVisible() { context.pendingJumpChapterId = null; }, setChromeHidden() {}, progressImageState: () => "ready",
    saveProgressNow: saveProgress || (async () => events.push(["save", context.currentChapterId, context.currentPageIndex])),
    positionAtImage: async (img) => { events.push(["position", Number(img.dataset.page)]); return true; },
    appendChapter: async ({ id }) => { events.push(["open", id]); return false; },
    scheduleSaveProgress() { events.push(["schedule", context.currentChapterId, context.currentPageIndex]); },
    updateUrlState(id, page) { events.push(["url", id, page]); },
  };
  vm.createContext(context);
  vm.runInContext(`${jump}\n${boundary}`, context);
  return { context, events };
}

test("selecting a chapter whose recap was skipped restores the first rendered page", async () => {
  const firstImg = { dataset: { page: "3" }, getBoundingClientRect: () => ({ height: 900 }) };
  const { context, events } = reader({ chapters: [{ id: "next", firstImg }] });
  await context.jumpToChapter({ id: "next", label: "2화" });
  assert.deepEqual(events, [["save", "old", 4], ["position", 3], ["schedule", "next", 3], ["url", "next", 3]]);
  await context.moveToChapterBoundary(false);
  assert.deepEqual(events.slice(-2), [["position", 3], ["schedule", "next", 3]]);
});

test("chapter start uses page zero when the reader revealed the skipped recap", async () => {
  const pageZero = { dataset: { page: "0" }, getBoundingClientRect: () => ({ height: 900 }) };
  const { context, events } = reader({ chapters: [{ id: "old", firstImg: { dataset: { page: "3" } } }], shownPageZero: pageZero });
  await context.moveToChapterBoundary(false);
  assert.deepEqual(events, [["position", 0]]);
});

test("a failed chapter selection never schedules a write for the unseen target", async () => {
  const { context, events } = reader();
  await context.jumpToChapter({ id: "unavailable", label: "3화" });
  assert.deepEqual(events, [["save", "old", 4], ["open", "unavailable"]]);
});

test("the latest rapid selection wins while the previous cursor is saving", async () => {
  const saves = [];
  const { context, events } = reader({ saveProgress: () => new Promise(resolve => saves.push(resolve)) });
  const first = context.jumpToChapter({ id: "slow", label: "2화" });
  const second = context.jumpToChapter({ id: "chosen", label: "3화" });
  saves[1](true);
  saves[0](true);
  await Promise.all([first, second]);
  assert.deepEqual(events, [["open", "chosen"]]);
});
