// 수집한 파일의 목록과 읽음 상태는 같은 주소의 뷰어 API에서 가져온다.
let libraryItems = [];
let libraryReady = false;
let libraryViewerPath = null;
let libraryReaderPath = null;
let libraryGeneration = 0;
let libraryRequest = null;

function libraryLocalUrl(raw) {
  const url = workflowReaderUrl(raw);
  return url && url.origin === window.location.origin && libraryViewerPath
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
    const placeholder = document.createElement("div");
    placeholder.className = "library-cover library-cover-placeholder";
    placeholder.textContent = "표지 없음";
    const coverUrl = libraryLocalUrl(item.cover_url);
    if (coverUrl) {
      const cover = document.createElement("img");
      cover.className = "library-cover";
      cover.alt = `${item.title} 표지`;
      cover.loading = "lazy";
      cover.src = coverUrl.href;
      cover.addEventListener("error", () => cover.replaceWith(placeholder), { once: true });
      card.append(cover);
    } else card.append(placeholder);
    const body = document.createElement("div");
    body.className = "library-card-body";
    const badge = document.createElement("span");
    badge.className = "library-platform";
    badge.textContent = { naver: "네이버", kakao: "카카오페이지", archive: "보관함" }[item.platform] || item.platform || "웹툰";
    const title = document.createElement("h3");
    title.textContent = item.title;
    const counts = document.createElement("p");
    counts.textContent = `총 ${libraryCount(item.chapter_count)}회차 · 안 읽은 회차 ${libraryCount(item.unread_count)}개`;
    const summary = document.createElement("p");
    summary.className = "library-summary";
    summary.textContent = libraryText(item.summary) || "소개글이 없습니다.";
    const details = document.createElement("dl");
    details.className = "library-card-details";
    const latest = libraryDetail("최근 업데이트", libraryText(item.latest_chapter?.label) || "회차 정보 없음", "library-latest-chapter");
    const date = libraryChapterDate(item.latest_chapter);
    const updated = document.createElement(date ? "time" : "span");
    updated.className = "library-chapter-date";
    updated.textContent = date ? date.label : "날짜 정보 없음";
    if (date) updated.dateTime = date.datetime;
    latest.querySelector("dd").appendChild(updated);
    const recentlyRead = libraryText(item.recently_read_chapter?.label)
      || (item.recently_read_chapter === undefined ? libraryText(item.progress_display) : "");
    details.append(latest, libraryDetail("최근 읽은 회차", recentlyRead || "아직 읽지 않음", "library-recently-read"));
    body.append(badge, title, summary, counts, details);
    if (item.progress_display) {
      const progress = document.createElement("p");
      progress.textContent = `${item.reading_now ? "읽는 중 · " : ""}${item.progress_display}`;
      body.append(progress);
    }
    const read = makeButton(item.reading_now ? "이어 읽기" : "읽기", () => {
      const url = libraryLocalUrl(libraryReaderPath);
      if (!url) return;
      url.searchParams.set("series", item.id);
      openEmbeddedReaderByUrl(item.title, url.href);
    });
    read.type = "button";
    read.setAttribute("aria-label", `${item.title} ${item.reading_now ? "이어 읽기" : "읽기"}`);
    body.append(read);
    card.append(body);
    grid.append(card);
  }
}

async function loadLibraryPage() {
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
    const response = await fetch(`${libraryViewerPath}/api/series`, { credentials: "same-origin", redirect: "manual", cache: "no-store", signal: controller.signal });
    if (generation !== libraryGeneration) return;
    if (response.status === 401 || response.status === 403 || response.type === "opaqueredirect") {
      status.textContent = "서재 인증이 필요합니다. 로그인한 뒤 서재를 새로고침해주세요.";
      login.href = `${libraryViewerPath}/login.html`;
      login.classList.remove("hidden");
      return;
    }
    if (!response.ok) throw new Error(`서재를 불러오지 못했습니다 (${response.status}). 다시 시도해주세요.`);
    const data = await response.json();
    if (generation !== libraryGeneration) return;
    if (!Array.isArray(data)) throw new Error("서재 목록 응답을 확인해주세요.");
    libraryItems = data.filter((item) => item && typeof item.id === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(item.id)
      && typeof item.title === "string");
    libraryReady = true;
    renderLibrary();
  } catch (error) {
    if (generation !== libraryGeneration) return;
    status.classList.add("workflow-error");
    status.textContent = error.name === "AbortError" ? "서재 응답이 늦어지고 있습니다. 새로고침으로 다시 시도해주세요." : error.message;
  } finally {
    clearTimeout(timeout);
    if (generation === libraryGeneration) { refresh.disabled = false; libraryRequest = null; }
  }
}

for (const id of ["library-search", "library-platform", "library-sort"]) document.getElementById(id).addEventListener(id === "library-search" ? "input" : "change", renderLibrary);
document.getElementById("library-refresh").addEventListener("click", loadLibraryPage);
document.addEventListener("webtoon-reader-closed", () => {
  if (!document.getElementById("page-library").classList.contains("hidden")) loadLibraryPage();
});
