const { makeApp, idle, waitFor, ok, done } = require("./harness");

(async () => {
  const config = { enabled: true, public_path: "/viewer", reader_url: "/viewer/reader.html" };
  let series = [
    { id: "naver_123", platform: "naver", title: "오늘만 사는 기사", chapter_count: 3, unread_count: 2, reading_now: true, progress_display: "1화 50%", latest_update: 20, cover_url: "/viewer/api/cover/naver_123",
      summary: "매일 같은 하루를 반복하는 기사가 자신의 한계를 넘어 성장하는 이야기입니다.",
      latest_chapter: { id: "ch3", label: "3화 · 세 번째 하루", date: "2026-11-13T16:30:00Z", date_source: "file_modified" },
      recently_read_chapter: { id: "ch1", label: "1화 · 첫 번째 하루" } },
    { id: "kakao_456", platform: "kakao", title: "광마회귀", chapter_count: 4, unread_count: 4, reading_now: false, latest_update: 10, cover_url: "https://unrelated.invalid/cover.jpg",
      summary: "<img src=x onerror=alert(1)>", latest_chapter: { id: "ch4", label: "4화", date: "2026-11-13T00:00:00Z", date_source: "unknown" }, recently_read_chapter: null },
    { id: "bad/id", platform: "naver", title: "잘못된 식별자" },
    { id: "archive_789", platform: "archive", title: "<img src=x onerror=alert(1)>", chapter_count: -3, unread_count: "bad", latest_update: 0 },
  ];
  const routes = {
    "/api/naver-list": [], "/api/settings/kakao-webtoons-enabled": { enabled: true },
    "/api/workflow/status": { progress: { state: "idle" }, subscriptions: [] },
    "/api/viewer/config": config,
    "/viewer/api/series": () => series,
    "/api/settings/webtoon-server": { webtoon_server_url: "/viewer", integrated: true, read_only: true },
  };
  const { w, run, calls } = makeApp(routes); await idle();
  const $ = (id) => w.document.getElementById(id);
  const cards = () => [...$("library-grid").querySelectorAll(".library-card")];
  const seriesCalls = () => calls.filter((call) => call.url === "/viewer/api/series").length;
  const change = (id, value, event = "change") => { $(id).value = value; $(id).dispatchEvent(new w.Event(event)); };
  const message = (origin, source, type, extra = {}) => w.dispatchEvent(new w.MessageEvent("message", { origin, source, data: { source: "webtoon-reader", version: 1, type, ...extra } }));

  w.document.querySelector('[data-page="manual-download"]').click();
  $("workspace-library").click(); await idle();
  ok(!$("page-library").classList.contains("hidden") && w.document.querySelector(".main-tabs").classList.contains("hidden") && $("workflow-overview").classList.contains("hidden"), "내 서재는 같은 화면에서 열리고 수집 전용 메뉴·현황을 숨김");
  ok($("workspace-library").getAttribute("aria-pressed") === "true" && w.sessionStorage.getItem("activeMainTab") === "library", "상위 메뉴 선택 상태와 기존 탭 복원 키를 함께 유지");
  ok(seriesCalls() === 1 && calls.some((call) => call.url === "/api/viewer/config"), "같은 주소의 연결 정보와 실제 뷰어 서재 API로 목록 조회");
  ok(cards().length === 3 && cards()[0].dataset.seriesId === "naver_123" && cards()[0].textContent.includes("총 3회차 · 안 읽은 회차 2개") && cards()[0].textContent.includes("1화 50%"), "다운로드 수·안 읽은 수·이어 읽기 위치와 최근 업데이트순 서재 표시");
  ok(cards()[0].querySelector("img").src === "http://localhost/viewer/api/cover/naver_123" && cards()[1].querySelector(".library-cover-placeholder"), "같은 뷰어 표지만 사용하고 외부 표지는 기본 이미지로 대체");
  ok(cards()[0].querySelector(".library-summary").textContent === series[0].summary && cards()[0].querySelector(".library-latest-chapter").textContent.includes("3화 · 세 번째 하루"), "내 서재 카드에 작품 소개와 최근 업데이트 회차명을 표시");
  ok(cards()[0].querySelector("time").textContent === "파일 갱신 2026.11.14" && cards()[0].querySelector("time").dateTime === "2026-11-13T16:30:00.000Z" && !cards()[0].textContent.includes("연재일"), "파일 수정 날짜는 한국 시간으로 표시하고 원작 연재일과 구분");
  ok(cards()[0].querySelector(".library-recently-read").textContent.includes("1화 · 첫 번째 하루") && cards()[0].querySelector("button").textContent === "이어 읽기", "최근 읽은 회차명과 기존 이어 읽기 버튼을 함께 유지");
  ok(cards()[1].querySelector(".library-summary").textContent === "<img src=x onerror=alert(1)>" && !cards()[1].querySelector("img"), "소개글의 HTML은 실행하지 않고 일반 텍스트로 출력");
  ok(cards()[1].querySelector(".library-recently-read").textContent.includes("아직 읽지 않음") && cards()[1].querySelector(".library-latest-chapter").textContent.includes("날짜 정보 없음") && !cards()[1].querySelector("time"), "읽음 기록과 날짜 출처가 없으면 임의의 기록이나 날짜를 만들지 않음");
  ok(cards()[2].querySelector(".library-summary").textContent === "소개글이 없습니다." && cards()[2].querySelector(".library-latest-chapter").textContent.includes("회차 정보 없음"), "소개와 최신 회차 정보가 없는 기존 서재 항목도 안내와 함께 표시");
  run('window.__invalidLibraryDate = libraryChapterDate({date:"not-a-date",date_source:"file_modified"});');
  ok(w.__invalidLibraryDate === null, "잘못된 날짜는 렌더링 예외나 Invalid Date 문구를 만들지 않음");
  ok(cards()[2].querySelector("h3").textContent === "<img src=x onerror=alert(1)>" && !cards()[2].querySelector("img") && cards()[2].textContent.includes("총 0회차"), "제목은 HTML로 실행하지 않고 잘못된 회차 수·식별자를 안전하게 처리");
  cards()[0].querySelector("img").dispatchEvent(new w.Event("error"));
  ok(cards()[0].querySelector(".library-cover-placeholder"), "표지 로드 실패 시 빈 이미지 대신 기본 표지 표시");

  const countBeforeFilter = seriesCalls();
  change("library-search", "광마", "input");
  ok(cards().length === 1 && cards()[0].dataset.seriesId === "kakao_456", "내 서재 제목 검색은 받은 작품만 즉시 필터링");
  change("library-platform", "naver");
  ok(!cards().length && $("library-status").textContent.includes("검색 조건"), "플랫폼과 제목 필터의 빈 결과를 안내");
  change("library-search", "", "input"); change("library-platform", ""); change("library-sort", "unread");
  ok(cards()[0].dataset.seriesId === "kakao_456" && seriesCalls() === countBeforeFilter, "안 읽은 회차 정렬·로컬 필터는 추가 스캔이나 API 호출을 하지 않음");
  change("library-sort", "latest");

  cards()[0].querySelector("button").click();
  const frame = $("workflow-reader-frame");
  let readerUrl = new URL(frame.src);
  ok($("workflow-reader-dialog").open && readerUrl.origin === "http://localhost" && readerUrl.pathname === "/viewer/reader.html" && readerUrl.searchParams.get("series") === "naver_123" && !readerUrl.searchParams.has("chapter"), "이어 읽기는 단일 주소의 리더에 작품만 전달하여 저장된 위치로 진입");
  ok(readerUrl.searchParams.get("embed") === "1" && readerUrl.searchParams.get("parent_origin") === "http://localhost" && $("workflow-reader-external").target === "_self", "내장 리더는 부모 origin을 명시하고 같은 주소의 복구 링크를 사용");
  const countBeforeClose = seriesCalls();
  $("workflow-reader-close").click();
  message("http://evil.invalid", frame.contentWindow, "close");
  message("http://localhost", w, "close");
  ok($("workflow-reader-dialog").open && !!frame.getAttribute("src") && $("workflow-reader-close").disabled && seriesCalls() === countBeforeClose, "읽기 닫기는 저장 응답을 기다리고 잘못된 origin·source 메시지로 갱신하지 않음");
  series = [{ ...series[0], unread_count: 1, progress_display: "2화 30%", recently_read_chapter: { id: "ch2", label: "2화 · 두 번째 하루" } }, ...series.slice(1)];
  message("http://localhost", frame.contentWindow, "close"); await idle();
  ok(!$("workflow-reader-dialog").open && !frame.getAttribute("src") && seriesCalls() === countBeforeClose + 1 && cards()[0].textContent.includes("안 읽은 회차 1개") && cards()[0].textContent.includes("2화 30%"), "저장 완료 응답으로 닫은 뒤 서재를 한 번 갱신해 새 읽음 상태 표시");
  ok(cards()[0].querySelector(".library-recently-read").textContent.includes("2화 · 두 번째 하루"), "내장 리더를 닫고 저장된 최신 읽음 회차를 서재 카드에 반영");
  cards()[0].querySelector("button").click(); $("workflow-reader-close").click();
  const countBeforeFailure = seriesCalls();
  message("http://localhost", frame.contentWindow, "error", { code: "progress_save_failed", message: "읽은 위치 저장 실패" }); await idle();
  ok($("workflow-reader-dialog").open && !!frame.getAttribute("src") && !$("workflow-reader-close").disabled && seriesCalls() === countBeforeFailure, "읽음 저장 실패 시 리더·위치를 유지하고 서재 갱신으로 오류를 숨기지 않음");
  $("workflow-reader-close").click(); message("http://localhost", frame.contentWindow, "close"); await idle();

  $("workspace-collection").click();
  ok(!$("page-manual-download").classList.contains("hidden") && !w.document.querySelector(".main-tabs").classList.contains("hidden") && !$("workflow-overview").classList.contains("hidden") && $("workspace-collection").getAttribute("aria-pressed") === "true", "수집으로 돌아오면 이전 수집 탭과 기존 메뉴·현황을 복원");
  run("window.__setting = loadWebtoonServerUrl();"); await w.__setting;
  ok($("webtoon-server-url").value === "/viewer" && $("webtoon-server-url").disabled && $("btn-save-webtoon-server-url").disabled && $("viewer-connection-hint").textContent.includes("같은 주소"), "통합 연결 주소는 읽기 전용으로 표시하여 이전 별도 포트 주소를 노출·변경하지 않음");
  routes["/api/settings/webtoon-server"] = { webtoon_server_url: "http://viewer.example" };
  run("window.__setting = loadWebtoonServerUrl();"); await w.__setting;
  ok(!$("webtoon-server-url").disabled && !$("btn-save-webtoon-server-url").disabled && $("webtoon-server-url").value === "http://viewer.example", "기존 독립 실행 모드는 뷰어 주소 편집 기능을 유지");

  series = []; $("workspace-library").click(); await idle();
  ok(!cards().length && $("library-status").textContent.includes("아직 받은 웹툰이 없습니다") && !$("library-refresh").disabled, "빈 서재는 수집·다운로드 안내와 다시 불러오기 제공");
  const originalFetch = w.fetch;
  let responseStatus = 401;
  w.fetch = (url, options) => String(url) === "/viewer/api/series"
    ? Promise.resolve({ ok: false, status: responseStatus, json: async () => ({}) }) : originalFetch(url, options);
  for (const status of [401, 403]) {
    responseStatus = status; $("library-refresh").click(); await idle();
    ok($("library-status").textContent.includes("인증이 필요") && !$("library-login").classList.contains("hidden") && $("library-login").getAttribute("href") === "/viewer/login.html" && !cards().length, `${status} 응답은 기존 뷰어 로그인 경로를 제공하고 오류를 빈 서재로 표시하지 않음`);
  }
  responseStatus = 502; $("library-refresh").click(); await idle();
  ok($("library-status").textContent.includes("502") && $("library-login").classList.contains("hidden") && !$("library-refresh").disabled, "뷰어 연결 실패는 상태 코드와 재시도 버튼으로 안내");
  change("library-search", "오류 중 입력", "input");
  ok($("library-status").textContent.includes("502"), "조회 실패 상태에서 검색 입력으로 오류 안내를 지우지 않음");
  w.fetch = originalFetch;
  series = { items: [] }; $("library-refresh").click(); await idle();
  ok($("library-status").textContent.includes("목록 응답"), "서재 API 형식 오류를 빈 결과로 삼키지 않음");
  routes["/api/viewer/config"] = { enabled: false }; $("library-refresh").click(); await idle();
  ok($("library-status").textContent.includes("연결이 준비되지 않았습니다"), "독립 실행의 미연결 서재에는 연결 상태를 안내");
  routes["/api/viewer/config"] = { ...config, reader_url: "https://evil.invalid/reader.html" }; $("library-refresh").click(); await idle();
  ok($("library-status").textContent.includes("읽기 주소"), "서재의 다른 origin 리더 주소를 열지 않음");
  routes["/api/viewer/config"] = config;
  change("library-search", "", "input");
  let resolveSlow;
  let invocation = 0;
  routes["/viewer/api/series"] = () => ++invocation === 1 ? new Promise((resolve) => { resolveSlow = resolve; }) : [{ id: "new_series", title: "최근 요청", chapter_count: 1 }];
  run("window.__slowLibrary = loadLibraryPage();");
  await waitFor(() => Boolean(resolveSlow), "느린 서재 요청 시작");
  ok($("library-refresh").disabled && $("library-status").textContent.includes("불러오는 중") && !cards().length, "조회 중에는 이전 결과를 지우고 로딩 상태를 표시");
  run("window.__newLibrary = loadLibraryPage();"); await w.__newLibrary;
  resolveSlow([{ id: "stale_series", title: "지난 요청" }]); await w.__slowLibrary; await idle();
  ok(cards().length === 1 && cards()[0].dataset.seriesId === "new_series" && !$("library-refresh").disabled, "늦게 끝난 이전 요청이 최신 서재 목록을 덮어쓰지 않음");
  ok(!calls.some((call) => call.method !== "GET"), "서재 탐색·읽기 진입은 구독·다운로드·파일·스캔 설정을 변경하지 않음");
  w.close();
  done();
})();
