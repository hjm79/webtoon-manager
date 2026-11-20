const assert = require("node:assert/strict");
const test = require("node:test");
const { createLoader, validSize } = require("../../static/reader-images.js");

function fixture(options = {}) {
  const callbacks = new Map();
  const observed = [];
  const viewportObserved = [];
  const timers = new Map();
  let timerId = 0;
  const loaded = [];
  const failed = [];
  let disconnected = 0;
  const loader = createLoader({
    observerFactory(callback, config) {
      assert.ok(["1200px 0px 3600px", "0px"].includes(config.rootMargin));
      const viewport = config.rootMargin === "0px";
      callbacks.set(viewport ? "viewport" : "near", callback);
      return {
        observe: (img) => (viewport ? viewportObserved : observed).push(img),
        unobserve() {},
        disconnect() { disconnected++; },
      };
    },
    schedule(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    cancel(id) { timers.delete(id); },
    onLoaded: (img) => loaded.push(img),
    onFailed: (img, retry) => failed.push({ img, retry }),
    ...options,
  });
  function image(number, top = 0) {
    const requests = [];
    const img = {
      dataset: {}, style: {}, isConnected: true, naturalWidth: 0, naturalHeight: 0,
      ownerDocument: { documentElement: { clientHeight: 800 } },
      requests, removed: [],
      moveTo(nextTop) { top = nextTop; },
      getBoundingClientRect() { return { top, bottom: top + 1600, height: 1600 }; },
      set src(value) { requests.push(value); },
      removeAttribute(name) { this.removed.push(name); },
      succeed(width = 720, height = 1600) { this.naturalWidth = width; this.naturalHeight = height; this.onload?.(); },
      fail() { this.onerror?.(); },
    };
    loader.register(img, `/viewer/api/chapters/ch/pages/${number}`, { width: 720, height: 1600 });
    return img;
  }
  function notify(field, isIntersecting, imgs) {
    callbacks.get(field)(imgs.map((target) => ({ target, isIntersecting })));
  }
  return { loader, image, observed, viewportObserved, loaded, failed, timers,
    visible: (...imgs) => notify("near", true, imgs),
    hidden: (...imgs) => notify("near", false, imgs),
    enterViewport: (...imgs) => notify("viewport", true, imgs),
    tick() {
      const first = [...timers].sort((a, b) => a[1].delay - b[1].delay)[0];
      if (!first) return;
      timers.delete(first[0]);
      first[1].callback();
    },
    disconnected: () => disconnected === 2,
  };
}

test("page dimensions reserve geometry without making an image request", () => {
  const f = fixture(); const img = f.image(0);
  assert.equal(img.width, 720); assert.equal(img.height, 1600);
  assert.equal(img.style.aspectRatio, "720 / 1600");
  assert.equal(img.dataset.dimensionsKnown, "true");
  assert.equal(img.requests.length, 0);
  assert.deepEqual(f.observed, [img]);
  assert.deepEqual(f.viewportObserved, [img]);
});

test("only viewport-near images are queued and at most three run concurrently", async () => {
  const f = fixture(); const imgs = Array.from({ length: 6 }, (_, i) => f.image(i));
  f.visible(...imgs.slice(0, 5));
  assert.deepEqual(imgs.map((img) => img.requests.length), [1, 1, 1, 0, 0, 0]);
  imgs[1].succeed();
  assert.deepEqual(imgs.map((img) => img.requests.length), [1, 1, 1, 1, 0, 0]);
  assert.equal(await f.loader.ensure(imgs[1]), true);
});

test("the restore target takes precedence over queued neighboring pages", () => {
  const f = fixture(); const imgs = Array.from({ length: 6 }, (_, i) => f.image(i));
  f.visible(...imgs.slice(0, 5));
  f.loader.ensure(imgs[5], { priority: true });
  imgs[0].succeed();
  assert.equal(imgs[5].requests.length, 1);
  assert.equal(imgs[3].requests.length, 0);
});

test("lookahead starts closest panels first and reserves capacity for a panel reaching the screen", () => {
  const f = fixture();
  const far = f.image(3, 3800);
  const near = f.image(1, 1000);
  const middle = f.image(2, 2500);
  f.visible(far, near, middle);
  assert.deepEqual([near, middle, far].map((img) => img.requests.length), [1, 1, 0]);
  assert.equal(near.fetchPriority, "low");

  far.moveTo(0);
  f.enterViewport(far);
  assert.equal(far.requests.length, 1);
  assert.equal(far.fetchPriority, "high");
  const next = f.image(4, 100);
  f.enterViewport(next);
  assert.equal(next.requests.length, 0);
  near.succeed();
  assert.equal(next.requests.length, 1);
});

test("an in-flight lookahead panel receives visible priority when it enters the screen", () => {
  const f = fixture(); const img = f.image(0, 1000);
  f.visible(img);
  assert.equal(img.fetchPriority, "low");
  img.moveTo(0);
  f.enterViewport(img);
  assert.equal(img.fetchPriority, "high");
  assert.equal(img.requests.length, 1);
});

test("newly visible pages bypass observer-only requests that have left the viewport", () => {
  const f = fixture(); const imgs = Array.from({ length: 9 }, (_, i) => f.image(i));
  f.visible(...imgs.slice(0, 8));
  f.hidden(...imgs.slice(0, 8));
  f.visible(imgs[8]);
  assert.ok(imgs.slice(3, 8).every((img) => img.dataset.imageState === "idle"));
  assert.ok(imgs.slice(0, 3).every((img) => img.dataset.imageState === "loading"));
  assert.ok(imgs.slice(0, 3).every((img) => img.removed.length === 0));
  imgs[0].succeed();
  assert.equal(imgs[8].requests.length, 1);
  assert.ok(imgs.slice(3, 8).every((img) => img.requests.length === 0));
  f.visible(imgs[4]);
  imgs[1].succeed();
  assert.equal(imgs[4].requests.length, 1);
});

test("explicitly ensured queued pages stay pinned after observer demand leaves", async () => {
  const f = fixture({ concurrency: 1 }); const a = f.image(0); const target = f.image(1);
  f.visible(a, target);
  const pending = f.loader.ensure(target, { priority: true });
  f.hidden(a, target);
  a.succeed();
  assert.equal(target.requests.length, 1);
  target.succeed();
  assert.equal(await pending, true);
});

test("an observer-only retry waits offscreen and resumes with the existing retry budget", () => {
  const f = fixture(); const img = f.image(0);
  f.visible(img); img.fail(); f.hidden(img); f.tick();
  assert.equal(img.requests.length, 1);
  assert.equal(img.dataset.imageState, "idle");
  f.visible(img);
  assert.equal(img.requests.length, 2);
  assert.match(img.requests[1], /retry=1$/);
  img.fail(); f.tick(); img.fail();
  assert.equal(img.requests.length, 3);
  assert.equal(img.dataset.imageState, "failed");
});

test("an explicit ensure during an offscreen retry retains a settling promise", async () => {
  const f = fixture(); const img = f.image(0);
  f.visible(img); img.fail(); f.hidden(img);
  const pending = f.loader.ensure(img);
  f.tick();
  assert.equal(img.requests.length, 2);
  img.succeed();
  assert.equal(await pending, true);
});

test("destroy settles pinned queued and retry-waiting requests", async () => {
  const f = fixture({ concurrency: 1 }); const a = f.image(0); const b = f.image(1); const c = f.image(2);
  const first = f.loader.ensure(a); a.fail();
  const second = f.loader.ensure(b); const third = f.loader.ensure(c);
  f.hidden(a, b, c); f.loader.destroy();
  assert.deepEqual(await Promise.all([first, second, third]), [false, false, false]);
  f.tick();
  assert.equal(a.requests.length, 1);
  assert.equal(c.requests.length, 0);
});

test("duplicate visibility notifications share the same image load", async () => {
  const f = fixture(); const img = f.image(0);
  const first = f.loader.ensure(img);
  assert.equal(first, f.loader.ensure(img));
  f.visible(img, img);
  assert.equal(img.requests.length, 1);
  img.succeed();
  assert.equal(await first, true);
  assert.deepEqual(f.loaded, [img]);
});

test("automatic recovery is bounded to the initial request plus two retries", async () => {
  const f = fixture(); const img = f.image(0); const result = f.loader.ensure(img);
  img.fail(); f.tick();
  img.fail(); f.tick();
  img.fail();
  assert.equal(await result, false);
  assert.equal(img.requests.length, 3);
  assert.equal(f.timers.size, 0);
  assert.equal(img.dataset.imageState, "failed");
  assert.equal(f.failed.length, 1);
  assert.match(img.requests[1], /retry=1$/);
  assert.match(img.requests[2], /retry=2$/);
  f.visible(img);
  assert.equal(img.requests.length, 3);
});

test("manual retry can recover a failed page without reloading other pages", async () => {
  const f = fixture({ maxRetries: 0 }); const img = f.image(0); const other = f.image(1);
  const first = f.loader.ensure(img); img.fail(); await first;
  const retry = f.failed[0].retry();
  assert.equal(img.requests.length, 2);
  assert.equal(other.requests.length, 0);
  img.succeed();
  assert.equal(await retry, true);
  assert.equal(img.dataset.imageState, "loaded");
});

test("a retry delay releases its concurrency slot for visible pages", () => {
  const f = fixture({ concurrency: 1 }); const a = f.image(0); const b = f.image(1);
  f.visible(a, b); a.fail();
  assert.equal(b.requests.length, 1);
  assert.equal(a.requests.length, 1);
});

test("a stalled request releases the slot and a late callback cannot finish its retry", async () => {
  const f = fixture({ concurrency: 1, loadTimeoutMs: 2000 });
  const stalled = f.image(0); const next = f.image(1);
  const result = f.loader.ensure(stalled);
  const expiredOnload = stalled.onload;
  f.enterViewport(next);
  f.tick();
  assert.deepEqual(stalled.removed, ["src"]);
  assert.equal(next.requests.length, 1);
  next.succeed();
  f.tick();
  assert.equal(stalled.requests.length, 2);
  expiredOnload();
  assert.equal(stalled.dataset.imageState, "loading");
  stalled.succeed();
  assert.equal(await result, true);
  assert.equal(f.timers.size, 0);
});

test("disconnected pages are skipped without consuming a request slot", async () => {
  const f = fixture({ concurrency: 1 }); const a = f.image(0); const b = f.image(1); const c = f.image(2);
  f.visible(a); const pending = f.loader.ensure(b); f.visible(c); b.isConnected = false;
  a.succeed();
  assert.equal(await pending, false);
  assert.equal(b.requests.length, 0);
  assert.equal(c.requests.length, 1);
});

test("destroying a chapter cancels retry timers and active image loads", async () => {
  const f = fixture(); const a = f.image(0); const b = f.image(1);
  const first = f.loader.ensure(a); const second = f.loader.ensure(b);
  a.fail(); f.loader.destroy();
  assert.equal(await first, false); assert.equal(await second, false);
  assert.equal(f.disconnected(), true);
  assert.deepEqual(b.removed, ["src"]);
  f.tick();
  assert.equal(a.requests.length, 1);
  assert.equal(await f.loader.ensure(a), false);
});

test("invalid dimensions cannot create a negative or nonfinite page layout", () => {
  for (const size of [null, {}, { width: -1, height: 10 }, { width: 720, height: Infinity }, { width: NaN, height: 10 }]) assert.equal(Boolean(validSize(size)), false);
  assert.equal(validSize({ width: 720, height: 1600 }), true);
});
