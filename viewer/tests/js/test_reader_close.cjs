const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { createWriter, createReadingEvidence } = require("../../static/reader-progress.js");
const html = fs.readFileSync(path.join(__dirname, "../../static/reader.html"), "utf8");
const helpers = html.match(/function setProgressSaveError\(visible\) \{[\s\S]*?\n\}/)[0];
const save = html.match(/function progressImageState\([\s\S]*?(?=\nfunction scheduleSaveProgress)/)[0];
const close = html.match(/async function leaveReaderForList\([\s\S]*?(?=\ndocument.getElementById\("backLink"\))/)[0];

function reader(fetcher, images = [{ dataset: { chapter: "chapter-7", page: "4" }, complete: true, naturalWidth: 720, naturalHeight: 1200 }]) {
  const messages = [];
  const requests = [];
  const banner = { hidden: true };
  const context = {
    Promise,
    encodeURIComponent,
    clearTimeout,
    currentChapterId: "chapter-7",
    pendingJumpChapterId: null,
    readingFromChapterId: "chapter-7", chapterIndexById: () => 0,
    completedChapterPosition: null, completedChapters: new Set(), persistedCompletedChapters: new Set(),
    readingEvidence: createReadingEvidence(),
    progressRevision: 1, savedProgressRevision: 0,
    closingProgressSaved: false,
    currentPageIndex: 4,
    seriesId: "series-1",
    saveTimer: null,
    embedBridge: { active: true },
    pagesEl: { querySelectorAll: () => images, classList: { contains: () => false } },
    document: { body: { classList: { remove() {} } }, getElementById: () => banner },
    stopAutoScroll() {}, stopContinuousScroll() {},
    exitBrowserFullscreen: async () => {},
    updateFullscreenButton: () => {},
    postReaderEvent: (type, payload) => messages.push({ type, ...payload }),
    fetch: async (url, options) => { requests.push({ url, options }); return fetcher(url, options); },
  };
  context.progressWriter = createWriter({ clientSessionId: "00000000-0000-4000-8000-000000000001",
    send: (body, { keepalive }) => context.fetch(`/api/series/${context.seriesId}/progress`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), keepalive,
    }) });
  vm.createContext(context);
  vm.runInContext(`${helpers}\n${save}\n${close}`, context);
  return { context, messages, requests, banner };
}

test("failed progress PUT keeps embedded reader open without close acknowledgement", async () => {
  const ctx = reader(async () => ({ ok: false, status: 500 }));
  await ctx.context.leaveReaderForList();
  assert.equal(ctx.banner.hidden, false);
  assert.equal(ctx.messages.some((m) => m.type === "close"), false);
  assert.deepEqual(ctx.messages.map((m) => m.type), ["error"]);
  assert.equal(ctx.messages[0].code, "progress_save_failed");
  assert.equal(ctx.context.currentChapterId, "chapter-7");
  assert.equal(ctx.context.currentPageIndex, 4);
  assert.equal(ctx.requests[0].options.keepalive, true);
});

test("network failure keeps embedded reader open and a retry saves the same position before close", async () => {
  let attempts = 0;
  const ctx = reader(async () => {
    attempts++;
    if (attempts === 1) throw new Error("connection lost");
    return { ok: true, status: 200 };
  });
  await ctx.context.leaveReaderForList();
  assert.equal(ctx.banner.hidden, false);
  assert.equal(ctx.messages.some((m) => m.type === "close"), false);
  await ctx.context.leaveReaderForList();
  assert.equal(ctx.banner.hidden, true);
  assert.deepEqual(ctx.messages.map((m) => m.type), ["error", "progress", "close"]);
  assert.deepEqual(ctx.requests.map((r) => (({ client_session_id, client_sequence, ...position }) => position)(JSON.parse(r.options.body))), [
    { chapter_id: "chapter-7", page_index: 4, completed: false, read_chapter_ids: [], reading_from_chapter_id: "chapter-7" },
    { chapter_id: "chapter-7", page_index: 4, completed: false, read_chapter_ids: [], reading_from_chapter_id: "chapter-7" },
  ]);
});

test("closing an idle reader does not write the old tab's cursor over a newer position", async () => {
  const ctx = reader(async () => assert.fail("an unchanged cursor must not be written"));
  ctx.context.progressRevision = 0;
  await ctx.context.leaveReaderForList();
  assert.equal(ctx.requests.length, 0);
  assert.deepEqual(ctx.messages.map((m) => m.type), ["close"]);
});

test("closing after an acknowledged autosave does not write the cursor again", async () => {
  const ctx = reader(async () => ({ ok: true }));
  assert.equal(await ctx.context.saveProgressNow(), true);
  await ctx.context.leaveReaderForList();
  assert.equal(ctx.requests.length, 1);
  assert.deepEqual(ctx.messages.map((m) => m.type), ["progress", "close"]);
});

test("close acknowledgement waits for the successful progress response", async () => {
  let finish;
  const ctx = reader(() => new Promise((resolve) => { finish = resolve; }));
  const closing = ctx.context.leaveReaderForList();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ctx.messages.length, 0);
  finish({ ok: true, status: 200 });
  await closing;
  assert.deepEqual(ctx.messages.map((m) => m.type), ["progress", "close"]);
});

test("non-close autosave callers receive false on failure without an unhandled rejection", async () => {
  const ctx = reader(async () => ({ ok: false, status: 500 }));
  assert.equal(await ctx.context.saveProgressNow(), false);
  assert.equal(ctx.messages.some((m) => m.type === "close"), false);
});

test("a pagehide save after an acknowledged embedded close cannot overwrite the saved position", async () => {
  const ctx = reader(async () => ({ ok: true, status: 200 }));
  await ctx.context.leaveReaderForList();
  assert.equal(ctx.context.closingProgressSaved, true);
  await ctx.context.saveProgressNow({ keepalive: true, urgent: true });
  assert.equal(ctx.requests.length, 1);
  assert.deepEqual(ctx.messages.map((m) => m.type), ["progress", "close"]);
});

test("closing a missing manifest, pending image, or corrupt target preserves the last server position", async () => {
  const pending = { dataset: { chapter: "chapter-7", page: "4", imageState: "loading" }, complete: false };
  const corrupt = { dataset: { chapter: "chapter-7", page: "4", imageState: "failed" }, complete: true, naturalWidth: 0, naturalHeight: 0 };
  for (const images of [[], [pending], [corrupt]]) {
    const ctx = reader(async () => ({ ok: true, status: 200 }), images);
    await ctx.context.leaveReaderForList();
    assert.equal(ctx.requests.length, 0);
    assert.deepEqual(ctx.messages.map((m) => m.type), ["close"]);
  }
});

test("retry button keeps normal Space activation instead of starting reader autoscroll", () => {
  const handler = html.match(/function handleScrollKeyDown\([\s\S]*?(?=\nfunction handleScrollKeyUp)/)[0];
  let intercepted = false;
  const context = { document: { getElementById: () => ({ classList: { contains: () => false } }) }, toggleAutoScroll: () => { intercepted = true; } };
  vm.createContext(context);
  vm.runInContext(handler, context);
  context.handleScrollKeyDown({ target: { tagName: "BUTTON" }, key: " ", code: "Space", preventDefault: () => { intercepted = true; } });
  assert.equal(intercepted, false);
});


test("stale 200 acknowledgement preserves a newer save error and never closes or announces persisted progress", async () => {
  const ctx = reader(async () => ({ ok: true, status: 200, json: async () => ({ ok: true, saved: false, reason: "stale_progress" }) }));
  ctx.banner.hidden = false;
  assert.equal(await ctx.context.saveProgressNow(), false);
  assert.equal(ctx.banner.hidden, false);
  assert.equal(ctx.messages.length, 0);
  await ctx.context.leaveReaderForList();
  assert.equal(ctx.banner.hidden, false);
  assert.equal(ctx.messages.length, 0);
});
