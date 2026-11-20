(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WebtoonReaderImages = api;
})(typeof window === "object" ? window : this, function () {
  function validSize(value) {
    return value && Number.isFinite(value.width) && Number.isFinite(value.height)
      && value.width > 0 && value.height > 0;
  }

  function createLoader({ observerFactory, concurrency = 3, maxRetries = 2,
                          loadTimeoutMs = 15_000,
                          schedule = setTimeout, cancel = clearTimeout,
                          onLoaded = () => {}, onFailed = () => {} }) {
    const records = new Map();
    const queue = new Set();
    const prefetchLimit = Math.max(1, concurrency - 1);
    let active = 0;
    let activePrefetch = 0;
    let disposed = false;
    const nearObserver = observerFactory((entries) => updateDemand(entries, "near"),
      { rootMargin: "1200px 0px 3600px" });
    const viewportObserver = observerFactory((entries) => updateDemand(entries, "inViewport"),
      { rootMargin: "0px" });

    function requested(record) {
      return record.near || record.inViewport || record.pinned;
    }

    function updateDemand(entries, field) {
      if (disposed) return;
      for (const entry of entries) {
        const record = records.get(entry.target);
        if (!record) continue;
        record[field] = entry.isIntersecting;
        if (requested(record) && record.state === "idle") enqueue(record);
        else if (!requested(record) && record.state === "queued") {
          queue.delete(record);
          record.state = "idle";
          record.img.dataset.imageState = "idle";
        }
        if (record.inViewport && record.state === "loading") record.img.fetchPriority = "high";
      }
      drain();
    }

    function enqueue(record) {
      record.state = "queued";
      record.img.dataset.imageState = "queued";
      queue.add(record);
    }

    function release(record) {
      active--;
      if (record.prefetch) activePrefetch--;
    }

    function finish(record, success) {
      release(record);
      record.state = success ? "loaded" : "failed";
      record.pinned = false;
      record.priority = false;
      nearObserver.unobserve(record.img);
      viewportObserver.unobserve(record.img);
      if (success) onLoaded(record.img);
      else if (!disposed && record.img.isConnected) onFailed(record.img, () => retry(record.img));
      record.resolve?.(success);
      record.resolve = null;
      drain();
    }

    function start(record, prefetch) {
      record.state = "loading";
      record.prefetch = prefetch;
      active++;
      if (prefetch) activePrefetch++;
      const img = record.img;
      const requestNumber = record.requests++;
      const isActive = () => !disposed && record.state === "loading" && record.requests === requestNumber + 1;
      const clearRequest = () => {
        if (record.deadline !== null) cancel(record.deadline);
        record.deadline = img.onload = img.onerror = null;
      };
      img.fetchPriority = prefetch ? "low" : "high";
      img.onload = () => {
        if (!isActive()) return;
        if (!img.naturalWidth || !img.naturalHeight) return fail();
        clearRequest();
        img.dataset.imageState = "loaded";
        img.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
        finish(record, true);
      };
      const fail = (timedOut = false) => {
        if (!isActive()) return;
        clearRequest();
        if (timedOut) img.removeAttribute?.("src");
        if (record.attempts++ < maxRetries) {
          release(record);
          record.state = "waiting";
          img.dataset.imageState = "waiting";
          record.timer = schedule(() => {
            record.timer = null;
            if (disposed || !img.isConnected) {
              record.state = "failed";
              record.pinned = false;
              record.resolve?.(false);
              record.resolve = null;
              return;
            }
            if (requested(record)) enqueue(record);
            else {
              record.state = "idle";
              img.dataset.imageState = "idle";
            }
            drain();
          }, 750 * record.attempts);
          drain();
        } else {
          img.dataset.imageState = "failed";
          finish(record, false);
        }
      };
      img.onerror = () => fail();
      img.dataset.imageState = "loading";
      record.deadline = schedule(() => fail(true), loadTimeoutMs);
      img.src = requestNumber ? `${record.url}${record.url.includes("?") ? "&" : "?"}retry=${requestNumber}` : record.url;
    }

    function distanceFromViewport(record) {
      const viewport = record.img.ownerDocument?.documentElement.clientHeight;
      const bounds = record.img.getBoundingClientRect?.();
      if (!viewport || !bounds?.height) return record.inViewport ? 0 : Infinity;
      return Math.max(bounds.top - viewport, -bounds.bottom, 0);
    }

    function drain() {
      while (!disposed && active < concurrency && queue.size) {
        let next, bestRank = Infinity, bestDistance = Infinity;
        for (const record of queue) {
          if (!record.img.isConnected) {
            queue.delete(record);
            record.state = "failed";
            record.pinned = false;
            record.resolve?.(false);
            record.resolve = null;
            continue;
          }
          const distance = distanceFromViewport(record);
          // Explicit restores and visible pages can use the slot reserved from lookahead.
          const rank = record.priority ? 0 : distance === 0 ? 1 : record.pinned ? 2 : 3;
          if (rank === 3 && activePrefetch >= prefetchLimit) continue;
          if (rank < bestRank || (rank === bestRank && distance < bestDistance)) {
            next = record;
            bestRank = rank;
            bestDistance = distance;
          }
        }
        if (!next) return;
        queue.delete(next);
        start(next, bestRank === 3);
      }
    }

    function ensure(img, { priority = false } = {}) {
      const record = records.get(img);
      if (!record || disposed) return Promise.resolve(false);
      if (record.state === "loaded") return Promise.resolve(true);
      if (record.state === "failed") return Promise.resolve(false);
      record.pinned = true;
      record.priority ||= priority;
      if (!record.promise) record.promise = new Promise((resolve) => { record.resolve = resolve; });
      if (record.state === "idle") enqueue(record);
      if (record.state === "loading" && priority) record.img.fetchPriority = "high";
      drain();
      return record.promise;
    }

    function retry(img) {
      const record = records.get(img);
      if (!record || disposed || record.state !== "failed") return ensure(img);
      record.attempts = 0;
      record.promise = null;
      record.state = "idle";
      return ensure(img, { priority: true });
    }

    function register(img, url, size) {
      img.dataset.imageState = "idle";
      if (validSize(size)) {
        img.width = size.width;
        img.height = size.height;
        img.style.aspectRatio = `${size.width} / ${size.height}`;
        img.dataset.dimensionsKnown = "true";
      } else {
        img.style.aspectRatio = "3 / 4";
      }
      records.set(img, { img, url, state: "idle", near: false, inViewport: false, pinned: false, priority: false,
        attempts: 0, requests: 0, promise: null, resolve: null, timer: null, deadline: null });
      nearObserver.observe(img);
      viewportObserver.observe(img);
    }

    function destroy() {
      disposed = true;
      nearObserver.disconnect();
      viewportObserver.disconnect();
      queue.clear();
      for (const record of records.values()) {
        if (record.timer !== null) cancel(record.timer);
        if (record.deadline !== null) cancel(record.deadline);
        record.img.onload = null;
        record.img.onerror = null;
        if (record.state === "loading") record.img.removeAttribute?.("src");
        record.resolve?.(false);
      }
      records.clear();
    }
    return { register, ensure, retry, destroy };
  }
  return { createLoader, validSize };
});
