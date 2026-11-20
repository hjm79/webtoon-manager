// 수집한 파일의 목록과 읽음 상태는 같은 주소의 뷰어 API에서 가져온다.
let libraryItems = [];
let libraryReady = false;
let libraryViewerPath = null;
let libraryReaderPath = null;
let libraryGeneration = 0;
let libraryRequest = null;
let libraryDetailItem = null;
let libraryDetailRequest = null;
const LIBRARY_VIEW_KEY = "webtoonLibraryView";
const EPISODE_ORDER_KEY = "webtoonEpisodeOrder";
const LIBRARY_ID = /^[A-Za-z0-9_-]{1,128}$/;
let libraryEpisodeOrder = "latest";

function libraryLocalUrl(raw) {
  const url = workflowReaderUrl(raw);
  return url && !url.username && !url.password && url.origin === window.location.origin && libraryViewerPath
    && url.pathname.startsWith(`${libraryViewerPath}/`) ? url : null;
}

function libraryCount(value) {
  const count = Number(value);
  return Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
}

function libraryUpdateTime(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : Date.parse(value) || 0;
}

function libraryText(value) {
  return typeof value === "string" ? value.trim() : "";
}

function libraryChapterDate(chapter) {
  if (chapter?.date_source !== "file_modified" || !libraryText(chapter.date)) return null;
  const date = new Date(chapter.date);
  if (!Number.isFinite(date.getTime())) return null;
  const label = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date).replace(/\s/g, "").replace(/\.$/, "");
  return { label: `파일 갱신 ${label}`, datetime: date.toISOString() };
}

function libraryDetail(label, text, className) {
  const row = document.createElement("div");
  row.className = className;
  const name = document.createElement("dt");
  name.textContent = label;
  const value = document.createElement("dd");
  value.textContent = text;
  row.append(name, value);
  return row;
}

function setLibraryView(view, save = true) {
  document.getElementById("library-grid").dataset.view = view;
  for (const mode of ["card", "list"]) {
    document.getElementById(`library-view-${mode}`).setAttribute("aria-pressed", String(mode === view));
  }
  if (save) {
    try { localStorage.setItem(LIBRARY_VIEW_KEY, view); } catch (_) { /* browsing still works without storage */ }
  }
}

function libraryCover(item, extraClass = "") {
  const placeholder = document.createElement("div");
  placeholder.className = `library-cover library-cover-placeholder ${extraClass}`.trim();
  placeholder.textContent = "표지 없음";
  const coverUrl = libraryLocalUrl(item.cover_url);
  if (!coverUrl) return placeholder;
  const cover = document.createElement("img");
  cover.className = `library-cover ${extraClass}`.trim();
  cover.alt = `${item.title} 표지`;
  cover.loading = "lazy";
  cover.src = coverUrl.href;
  cover.addEventListener("error", () => cover.replaceWith(placeholder), { once: true });
  return cover;
}

function libraryPlatform(item) {
  const badge = document.createElement("span");
  badge.className = "library-platform";
  badge.textContent = { naver: "네이버", kakao: "카카오페이지", archive: "보관함" }[item.platform] || item.platform || "웹툰";
  return badge;
}

function libraryFacts(item) {
  const latest = libraryDetail("최근 업데이트", libraryText(item.latest_chapter?.label) || "회차 정보 없음", "library-latest-chapter");
  const date = libraryChapterDate(item.latest_chapter);
  const updated = document.createElement(date ? "time" : "span");
  updated.className = "library-chapter-date";
  updated.textContent = date ? date.label : "날짜 정보 없음";
  if (date) updated.dateTime = date.datetime;
  latest.querySelector("dd").appendChild(updated);
  const recentlyRead = libraryText(item.recently_read_chapter?.label)
    || (item.recently_read_chapter === undefined ? libraryText(item.progress_display) : "");
  return [latest, libraryDetail("최근 읽은 회차", recentlyRead || "아직 읽지 않음", "library-recently-read")];
}

function openLibraryReader(item, chapterId) {
  const url = libraryLocalUrl(libraryReaderPath);
  if (!url) return;
  url.searchParams.set("series", item.id);
  url.searchParams.set("shelf_chapters", "1");
  url.searchParams.delete("page");
  if (chapterId) url.searchParams.set("chapter", chapterId);
  else url.searchParams.delete("chapter");
  openEmbeddedReaderByUrl(item.title, url.href);
}

function renderLibrary() {
  if (!libraryReady) return;
  const query = document.getElementById("library-search").value.trim().toLocaleLowerCase();
  const platform = document.getElementById("library-platform").value;
  const sort = document.getElementById("library-sort").value;
  const items = libraryItems.filter((item) => (!platform || item.platform === platform)
    && item.title.toLocaleLowerCase().includes(query));
  items.sort((a, b) => sort === "title" ? a.title.localeCompare(b.title, "ko")
    : sort === "unread" ? libraryCount(b.unread_count) - libraryCount(a.unread_count) || a.title.localeCompare(b.title, "ko")
      : libraryUpdateTime(b.latest_update) - libraryUpdateTime(a.latest_update) || a.title.localeCompare(b.title, "ko"));
  const grid = document.getElementById("library-grid");
  grid.replaceChildren();
  document.getElementById("library-status").textContent = items.length ? `웹툰 ${items.length}개`
    : libraryItems.length ? "검색 조건에 맞는 웹툰이 없습니다." : "아직 받은 웹툰이 없습니다. 수집에서 작품을 구독하고 회차를 받아 주세요.";
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "library-card";
    card.dataset.seriesId = item.id;
    card.append(libraryCover(item));
    card.addEventListener("click", (event) => {
      if (!event.target.closest("button, a")) openLibraryDetail(item);
    });
    const body = document.createElement("div");
    body.className = "library-card-body";
    const title = document.createElement("h3");
    const titleButton = makeButton(item.title, () => openLibraryDetail(item));
    titleButton.type = "button";
    titleButton.className = "library-title-button";
    titleButton.setAttribute("aria-label", `${item.title} 작품 정보와 회차`);
    title.append(titleButton);
    const counts = document.createElement("p");
    counts.className = "library-counts";
    counts.textContent = `총 ${libraryCount(item.chapter_count)}회차 · 안 읽은 회차 ${libraryCount(item.unread_count)}개`;
    const summary = document.createElement("p");
    summary.className = "library-summary";
    summary.textContent = libraryText(item.summary) || "소개글이 없습니다.";
    const details = document.createElement("dl");
    details.className = "library-card-details";
    details.append(...libraryFacts(item));
    body.append(libraryPlatform(item), title, summary, counts, details);
    if (item.progress_display) {
      const progress = document.createElement("p");
      progress.className = "library-progress";
      progress.textContent = `${item.reading_now ? "읽는 중 · " : ""}${item.progress_display}`;
      body.append(progress);
    }
    const read = makeButton(item.reading_now ? "이어 읽기" : "읽기", () => openLibraryReader(item));
    read.type = "button";
    read.className = "library-read-button";
    read.setAttribute("aria-label", `${item.title} ${item.reading_now ? "이어 읽기" : "읽기"}`);
    body.append(read);
    card.append(body);
    grid.append(card);
  }
}

function renderLibraryDetailInfo(item, info = {}) {
  const metadata = document.createElement("div");
  metadata.className = "library-detail-meta";
  const summary = document.createElement("p");
  summary.className = "library-detail-summary";
  summary.textContent = libraryText(info.summary) || libraryText(item.summary) || "소개글이 없습니다.";
  const facts = document.createElement("dl");
  facts.className = "library-card-details";
  for (const [key, label] of [["writer", "작가"], ["genre", "장르"], ["publisher", "출판사"]]) {
    const value = libraryText(info[key]);
    if (value) facts.append(libraryDetail(label, value, ""));
  }
  facts.append(...libraryFacts(item));
  metadata.append(libraryPlatform(item), summary, facts);
  document.getElementById("library-detail-info").replaceChildren(libraryCover(item, "library-detail-cover"), metadata);
}

function renderLibraryEpisodes(item, chapters) {
  const list = document.getElementById("library-episodes");
  const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.chapterId : null;
  const rows = document.createDocumentFragment();
  const ordered = libraryEpisodeOrder === "oldest" ? chapters : [...chapters].reverse();
  for (const chapter of ordered) {
    const row = document.createElement("li");
    row.className = "library-episode";
    const open = makeButton(chapter.label, () => openLibraryReader(item, chapter.id));
    open.type = "button";
    open.className = "library-episode-link";
    open.dataset.chapterId = chapter.id;
    const states = document.createElement("span");
    states.className = "library-episode-states";
    const hasRead = chapter.has_read === true || (chapter.has_read === undefined && chapter.read === true);
    const reading = chapter.reading === true;
    if (hasRead) states.append(libraryEpisodeBadge("읽음", "is-read"));
    if (reading) states.append(libraryEpisodeBadge("읽는 중", "is-reading"));
    if (!hasRead && !reading) states.append(libraryEpisodeBadge("안 읽음", ""));
    row.append(open, states);
    rows.append(row);
  }
  list.replaceChildren(rows);
  if (focusedId) list.querySelector(`[data-chapter-id="${focusedId}"]`)?.focus({ preventScroll: true });
}

function setLibraryEpisodeOrder(value, remember = false) {
  const order = value === "oldest" ? "oldest" : "latest";
  document.getElementById("library-episode-order").value = order;
  if (order !== libraryEpisodeOrder) {
    libraryEpisodeOrder = order;
    const list = document.getElementById("library-episodes");
    list.reversed = order === "latest";
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    for (const row of [...list.children].reverse()) list.appendChild(row);
    focused?.focus({ preventScroll: true });
  }
  if (remember) {
    try { localStorage.setItem(EPISODE_ORDER_KEY, order); } catch (_) { /* sorting works without storage */ }
  }
}

function libraryEpisodeBadge(label, state) {
  const badge = document.createElement("span");
  badge.className = `library-episode-badge ${state}`.trim();
  badge.textContent = label;
  return badge;
}

function openLibraryDetail(item) {
  libraryDetailItem = item;
  document.getElementById("library-browse").classList.add("hidden");
  document.getElementById("library-detail").classList.remove("hidden");
  const title = document.getElementById("library-detail-title");
  title.textContent = item.title;
  title.focus();
  loadLibraryDetail();
}

function closeLibraryDetail(restoreFocus = true) {
  if (!libraryDetailItem) return;
  const { id } = libraryDetailItem;
  libraryDetailItem = null;
  libraryDetailRequest?.abort();
  libraryDetailRequest = null;
  document.getElementById("library-detail").classList.add("hidden");
  document.getElementById("library-browse").classList.remove("hidden");
  if (restoreFocus) {
    const trigger = document.querySelector(`#library-grid [data-series-id="${id}"] .library-title-button`);
    (trigger || document.getElementById("library-search")).focus();
  }
}

function libraryFetch(url, signal) {
  return fetch(url, { credentials: "same-origin", redirect: "manual", cache: "no-store", signal });
}

function libraryNeedsLogin(response) {
  return response.status === 401 || response.status === 403 || response.type === "opaqueredirect";
}

async function loadLibraryDetail(keepEpisodes = false) {
  const item = libraryDetailItem;
  if (!item) return;
  libraryDetailRequest?.abort();
  const controller = new AbortController();
  libraryDetailRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 15000);
  const endpoint = `${libraryViewerPath}/api/series/${encodeURIComponent(item.id)}`;
  const status = document.getElementById("library-detail-status");
  const retry = document.getElementById("library-detail-retry");
  const login = document.getElementById("library-detail-login");
  const read = document.getElementById("library-detail-continue");
  const list = document.getElementById("library-episodes");
  renderLibraryDetailInfo(item);
  status.classList.remove("workflow-error");
  status.textContent = "회차를 불러오는 중…";
  retry.classList.add("hidden");
  login.classList.add("hidden");
  login.removeAttribute("href");
  read.disabled = true;
  read.textContent = item.reading_now ? "이어 읽기" : "읽기";
  if (!keepEpisodes) list.replaceChildren();

  const infoRequest = libraryFetch(`${endpoint}/info`, controller.signal).then(async (response) => {
    if (!response.ok) return; // older archives may not have an info.xml file
    const info = await response.json();
    if (libraryDetailRequest === controller && !controller.signal.aborted && info && typeof info === "object" && !Array.isArray(info)) {
      renderLibraryDetailInfo(item, info);
    }
  }).catch(() => {}); // list metadata remains visible if the optional lookup fails

  try {
    const response = await libraryFetch(`${endpoint}/chapters`, controller.signal);
    if (libraryDetailRequest !== controller) return;
    if (libraryNeedsLogin(response)) {
      login.href = `${libraryViewerPath}/login.html`;
      login.classList.remove("hidden");
      throw new Error("서재 인증이 필요합니다. 로그인한 뒤 다시 시도해주세요.");
    }
    if (!response.ok) throw new Error(`회차를 불러오지 못했습니다 (${response.status}). 다시 시도해주세요.`);
    const data = await response.json();
    if (libraryDetailRequest !== controller) return;
    if (data?.id !== item.id || !Array.isArray(data.chapters) || data.chapters.some((chapter) =>
      !chapter || typeof chapter.id !== "string" || !LIBRARY_ID.test(chapter.id) || typeof chapter.label !== "string")) {
      throw new Error("회차 목록 응답을 확인해주세요.");
    }
    renderLibraryEpisodes(item, data.chapters);
    read.disabled = data.chapters.length === 0;
    status.textContent = data.chapters.length ? `총 ${data.chapters.length}회차` : "아직 받은 회차가 없습니다.";
  } catch (error) {
    if (libraryDetailRequest !== controller) return;
    list.replaceChildren();
    status.classList.add("workflow-error");
    status.textContent = error.name === "AbortError" ? "회차 목록 응답이 늦어지고 있습니다. 다시 시도해주세요." : error.message;
    retry.classList.remove("hidden");
  } finally {
    await infoRequest;
    clearTimeout(timeout);
    if (libraryDetailRequest === controller) libraryDetailRequest = null;
  }
}

async function loadLibraryPage({ keepDetail = false } = {}) {
  if (!keepDetail) closeLibraryDetail(false);
  const generation = ++libraryGeneration;
  libraryReady = false;
  libraryItems = [];
  libraryRequest?.abort();
  const controller = new AbortController();
  libraryRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 15000);
  const status = document.getElementById("library-status");
  const login = document.getElementById("library-login");
  const refresh = document.getElementById("library-refresh");
  refresh.disabled = true;
  status.textContent = "서재를 불러오는 중…";
  status.classList.remove("workflow-error");
  login.classList.add("hidden");
  login.removeAttribute("href");
  document.getElementById("library-grid").replaceChildren();
  try {
    const config = await apiCall("/api/viewer/config", { signal: controller.signal });
    if (generation !== libraryGeneration) return;
    if (!config.enabled) {
      status.textContent = "내 서재 연결이 준비되지 않았습니다. 뷰어 연결 설정을 확인해주세요.";
      return;
    }
    const path = String(config.public_path || "");
    if (!/^\/(?!\/)[A-Za-z0-9_/-]+$/.test(path)) throw new Error("서재 연결 주소를 확인해주세요.");
    libraryViewerPath = path.replace(/\/+$/, "");
    libraryReaderPath = config.reader_url || `${libraryViewerPath}/reader.html`;
    if (!libraryLocalUrl(libraryReaderPath)) throw new Error("서재 읽기 주소를 확인해주세요.");
    const response = await libraryFetch(`${libraryViewerPath}/api/series`, controller.signal);
    if (generation !== libraryGeneration) return;
    if (libraryNeedsLogin(response)) {
      status.textContent = "서재 인증이 필요합니다. 로그인한 뒤 서재를 새로고침해주세요.";
      login.href = `${libraryViewerPath}/login.html`;
      login.classList.remove("hidden");
      return;
    }
    if (!response.ok) throw new Error(`서재를 불러오지 못했습니다 (${response.status}). 다시 시도해주세요.`);
    const data = await response.json();
    if (generation !== libraryGeneration) return;
    if (!Array.isArray(data)) throw new Error("서재 목록 응답을 확인해주세요.");
    libraryItems = data.filter((item) => item && typeof item.id === "string" && LIBRARY_ID.test(item.id)
      && typeof item.title === "string");
    libraryReady = true;
    renderLibrary();
    if (keepDetail && libraryDetailItem) {
      const selected = libraryItems.find((item) => item.id === libraryDetailItem.id);
      if (selected) libraryDetailItem = selected;
    }
  } catch (error) {
    if (generation !== libraryGeneration) return;
    status.classList.add("workflow-error");
    status.textContent = error.name === "AbortError" ? "서재 응답이 늦어지고 있습니다. 새로고침으로 다시 시도해주세요." : error.message;
  } finally {
    clearTimeout(timeout);
    if (generation === libraryGeneration) {
      refresh.disabled = false;
      libraryRequest = null;
      if (keepDetail && libraryDetailItem) loadLibraryDetail(true);
    }
  }
}

let savedLibraryView = "card";
try { if (localStorage.getItem(LIBRARY_VIEW_KEY) === "list") savedLibraryView = "list"; } catch (_) { /* default to cards */ }
setLibraryView(savedLibraryView, false);
try { setLibraryEpisodeOrder(localStorage.getItem(EPISODE_ORDER_KEY)); } catch (_) { /* default to newest */ }
document.getElementById("library-episode-order").addEventListener("change", (event) => setLibraryEpisodeOrder(event.target.value, true));
window.addEventListener("storage", (event) => {
  if (event.key === EPISODE_ORDER_KEY || event.key === null) setLibraryEpisodeOrder(event.newValue);
});
for (const mode of ["card", "list"]) document.getElementById(`library-view-${mode}`).addEventListener("click", () => setLibraryView(mode));
for (const id of ["library-search", "library-platform", "library-sort"]) document.getElementById(id).addEventListener(id === "library-search" ? "input" : "change", renderLibrary);
document.getElementById("library-refresh").addEventListener("click", loadLibraryPage);
document.getElementById("library-detail-back").addEventListener("click", () => closeLibraryDetail());
document.getElementById("library-detail-retry").addEventListener("click", () => loadLibraryDetail());
document.getElementById("library-detail-continue").addEventListener("click", () => {
  if (libraryDetailItem) openLibraryReader(libraryDetailItem);
});
document.addEventListener("webtoon-reader-closed", () => {
  if (!document.getElementById("page-library").classList.contains("hidden")) loadLibraryPage({ keepDetail: true });
});
