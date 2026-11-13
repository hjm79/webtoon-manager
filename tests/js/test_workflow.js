const { makeApp, idle, waitFor, ok, done } = require("./harness");

(async () => {
  let reader = { status: "not_downloaded", url: null };
  const saved = [];
  const scopeSaved = [];
  let scopeFailure = false;
  const routes = {
    "/api/naver-list": [], "/api/settings/kakao-webtoons-enabled": { enabled: true },
    "/api/settings/webtoon-server": { webtoon_server_url: "http://viewer" },
    "/api/workflow/status": { next_run_at: "2026-11-14T03:00:00+09:00", progress: { state: "idle" }, subscriptions: [] },
    "/api/workflow/search": { results: [{ platform: "naver", title_id: "824543", title: "오늘만 사는 기사", is_paused: true, subscription: null }], errors: [{ platform: "kakao", message: "로그인을 확인해주세요." }] },
    "/api/workflow/preview": { available_count: 2, episodes: [{ episode_no: 1, subtitle: "처음", available: true, owned: false }, { episode_no: 2, subtitle: "다음", available: true, owned: false }, { episode_no: 3, subtitle: "잠김", available: false, owned: false }] },
    "/api/workflow/subscribe": (opts) => { saved.push(JSON.parse(opts.body)); return { subscription: { status: "active", ever_subscribed: true }, execution: "scheduled", next_run_at: "2026-11-14T03:00:00+09:00" }; },
    "/api/workflow/scope": (opts) => {
      if (scopeFailure) throw new Error("다른 다운로드가 실행 중입니다. 완료 후 다시 시도해주세요.");
      const value = JSON.parse(opts.body); scopeSaved.push(value);
      routes["/api/workflow/status"] = { next_run_at: "2026-11-14T03:00:00+09:00", subscriptions: [{ title_id: value.title_id, title: "기존 작품", platform: value.platform, downloaded_count: 1, scope: value.scope }] };
      return { subscription: { status: "active" }, scope: value.scope, execution: "saved" };
    },
    "/api/reader/status": () => reader,
  };
  const { w, run, calls } = makeApp(routes); await idle();
  const $ = (id) => w.document.getElementById(id);
  const submit = () => $("workflow-subscribe-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  const mode = (value) => { const input = w.document.querySelector(`input[name="workflow-scope"][value="${value}"]`); input.checked = true; input.dispatchEvent(new w.Event("change")); };
  $("workflow-search-query").value = "오늘만 사는 기사";
  $("workflow-search-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true })); await idle();
  ok(calls.some((call) => call.url.startsWith("/api/workflow/search?query=")), "요일 목록 필터가 아닌 통합 검색 API 호출");
  ok($("workflow-search-results").textContent.includes("오늘만 사는 기사") && $("workflow-search-results").textContent.includes("휴재"), "휴재 작품 검색 결과 표시");
  ok($("workflow-search-status").textContent.includes("카카오페이지: 로그인을 확인"), "부분 플랫폼 실패를 결과와 함께 표시");
  const subscribe = $("workflow-search-results").querySelector("button");
  subscribe.click(); await idle();
  ok($("workflow-subscribe-dialog").open && saved.length === 0, "구독을 눌러도 범위 선택 전에는 등록·다운로드 요청 없음");
  ok(!$("workflow-start-now").checked && w.document.querySelector('input[name="workflow-scope"]:checked').value === "latest", "기본은 최근 10회차와 예약 등록");
  mode("all");
  ok($("workflow-scope-hint").textContent.includes("무료로 열리는 기존 회차도 다시 확인") && !$("workflow-scope-hint").textContent.includes("자동으로 추가하지 않습니다"), "전체 범위는 추후 무료로 열리는 기존 회차도 확인한다고 안내");
  mode("selected"); submit(); await idle();
  ok($("workflow-scope-hint").textContent.includes("선택하지 않은 기존 회차와 이후 새로 추가되는 회차") && $("workflow-scope-hint").textContent.includes("자동으로 포함하지 않습니다"), "선택 범위는 기존·새 회차 모두 자동 추가하지 않는 고정 목록이라고 안내");
  ok(saved.length === 0 && $("workflow-subscribe-error").textContent.includes("하나 이상"), "빈 선택은 구독 요청 전 거절");
  const picks = $("workflow-episode-picker").querySelectorAll("input");
  picks[1].checked = true;
  ok(picks[2].disabled, "잠긴 회차는 선택할 수 없음");
  $("workflow-start-now").checked = true; submit(); await idle();
  ok(saved.length === 1 && JSON.stringify(saved[0].scope) === '{"mode":"selected","episode_nos":[2]}' && saved[0].start_now === true, "고른 회차만 명시적으로 즉시 실행 요청");
  ok(!$("workflow-subscribe-dialog").open && subscribe.disabled, "등록 성공 시 모달을 닫고 구독 중 상태 표시");
  ok(!$("workflow-search-results").querySelector("[data-scope-title-id]").classList.contains("hidden"), "신규 구독 성공 후 검색 카드에 범위 변경 버튼 표시");

  const editingPreview = {
    subscription: "active", current_scope: { mode: "selected", episode_nos: [1, 3] }, available_count: 1,
    episodes: [
      { episode_no: 1, subtitle: "이미 받은 회차", available: false, owned: true },
      { episode_no: 2, subtitle: "선택할 무료 회차", available: true, owned: false },
      { episode_no: 3, subtitle: "기존 선택 잠김", available: false, owned: false },
      { episode_no: 4, subtitle: "새 잠김", available: false, owned: false },
    ],
  };
  routes["/api/workflow/preview"] = editingPreview;
  run('renderWorkflowStatus({subscriptions:[{title_id:"777",title:"기존 작품",platform:"kakao",downloaded_count:1,scope:{mode:"selected",episode_nos:[1,3]}}]});');
  const overviewEdit = $("workflow-subscriptions").querySelector("[data-scope-title-id]");
  ok(overviewEdit && overviewEdit.textContent === "범위 변경", "기존 구독 현황마다 범위 변경 버튼 표시");
  overviewEdit.click(); await idle();
  let editPicks = $("workflow-episode-picker").querySelectorAll("input");
  ok($("workflow-subscribe-title").textContent.includes("다운로드 범위 변경") && $("workflow-subscribe-submit").textContent === "범위 저장" && $("workflow-scope-legend").textContent === "앞으로 받을 회차", "편집은 구독과 구분한 제목·설명·저장 버튼 사용");
  ok(!$("workflow-preserve-hint").classList.contains("hidden") && $("workflow-preserve-hint").textContent.includes("읽음 기록"), "기존 파일·읽음 기록 유지 안내 표시");
  ok(w.document.querySelector('input[name="workflow-scope"]:checked').value === "selected" && editPicks[0].checked && editPicks[2].checked && !editPicks[1].checked, "기존 선택 범위를 정확히 복원");
  ok(!editPicks[0].disabled && !editPicks[2].disabled && editPicks[3].disabled, "이미 받은 회차와 기존 선택은 잠겨도 유지·해제 가능하고 새 잠긴 회차는 선택 불가");
  ok(!$("workflow-start-now").checked && $("workflow-start-hint").textContent.includes("기존 다운로드 예약을 유지"), "편집도 즉시 실행은 꺼진 상태로 시작하고 예약 보존 안내");
  $("workflow-subscribe-cancel").click();
  ok(scopeSaved.length === 0 && saved.length === 1, "범위 편집 취소 시 범위나 구독 변경 요청 없음");

  overviewEdit.click(); await idle();
  editPicks = $("workflow-episode-picker").querySelectorAll("input");
  editPicks[1].checked = true; editPicks[2].checked = false;
  submit(); await idle();
  ok(scopeSaved.length === 1 && JSON.stringify(scopeSaved[0]) === '{"platform":"kakao","title_id":"777","scope":{"mode":"selected","episode_nos":[1,2]},"start_now":false}', "기존 구독은 고른 범위만 scope API에 저장하고 즉시 다운로드하지 않음");
  ok(!$("workflow-subscribe-dialog").open && $("workflow-subscriptions").textContent.includes("선택한 회차만"), "저장 성공 후 모달을 닫고 현황의 범위 갱신");

  scopeFailure = true;
  $("workflow-subscriptions").querySelector("[data-scope-title-id]").click(); await idle();
  submit(); await idle();
  ok($("workflow-subscribe-dialog").open && $("workflow-subscribe-error").textContent.includes("다른 다운로드가 실행 중") && !$("workflow-subscribe-submit").disabled && scopeSaved.length === 1, "실행 중 범위 저장 거절 시 이유를 표시하고 편집 내용을 유지");
  $("workflow-subscribe-cancel").click(); scopeFailure = false;
  for (const currentMode of ["all", "latest", "future"]) {
    editingPreview.current_scope = { mode: currentMode, latest_count: currentMode === "latest" ? 7 : null, episode_nos: [] };
    run('window.__edit = chooseSubscriptionScope({platform:"kakao",title_id:"777",title:"기존 작품"},{edit:true});'); await idle();
    ok(w.document.querySelector('input[name="workflow-scope"]:checked').value === currentMode && (currentMode !== "latest" || $("workflow-latest-count").value === "7"), `기존 ${currentMode} 범위와 최근 회차 수 복원`);
    $("workflow-subscribe-cancel").click();
  }
  run('window.__activeCard = buildWebtoonCard({platform:"kakao",title_id:"777",title:"기존 작품",status:"active"},"naver-list");');
  ok(w.__activeCard.querySelector("[data-scope-title-id]")?.textContent === "범위 변경", "요일 목록의 기존 구독 카드에서도 범위 변경 가능");

  run('window.__scope = chooseSubscriptionScope({platform:"naver",title_id:"777",title:"다음 작품"});'); await idle();
  mode("future");
  ok($("workflow-start-now").disabled && !$("workflow-start-now").checked, "새 회차만 모드는 즉시 다운로드를 선택하지 않음");
  $("workflow-subscribe-cancel").click();
  ok(await w.__scope === null && saved.length === 1, "취소는 구독·다운로드 요청을 만들지 않음");
  run('renderWorkflowStatus({next_run_at:"2026-11-14T03:00:00+09:00",progress:{state:"resting",title:"작품",total:25,completed:10,failed:1,current_episode_no:10,rest_until:"2026-11-13T14:30:00+09:00"},subscriptions:[{title:"작품",platform:"naver",downloaded_count:10,scope:{mode:"latest",latest_count:10}}]});');
  ok($("workflow-state").textContent.includes("휴식") && $("workflow-progress-detail").textContent.includes("10/25") && $("workflow-progress-detail").textContent.includes("실패 1"), "진행·휴식·실패 수를 구조화된 상태에서 표시");
  ok($("workflow-next-run").textContent.includes("3:00") && $("workflow-subscriptions").textContent.includes("받은 회차 10개"), "다음 예약과 작품별 실제 받은 수 표시");
  run('renderWorkflowStatus({next_run_at:null,progress:null,subscriptions:[{title:"보유 작품",platform:"naver",status:"available",downloaded_count:1,scope:{mode:"latest",latest_count:1}},{title:"대기 작품",platform:"naver",status:"idle",downloaded_count:0,scope:{mode:"future"}}]});');
  ok($("workflow-subscriptions").textContent.includes("보유 작품 · 받은 회차 있음 · 받은 회차 1개") && !$("workflow-subscriptions").textContent.includes("예약 대기"), "재시작 후 파일이 있으면 완료나 예약 대기를 단정하지 않고 보유 상태 표시");
  ok($("workflow-subscriptions").textContent.includes("대기 작품 · 대기 중") && $("workflow-next-run").textContent.includes("등록된 다운로드 예약이 없습니다"), "예약 없는 대기 상태와 다음 예약 유무를 분리해 표시");

  run('window.__reader = openEmbeddedReader("작품","naver");'); await w.__reader;
  ok($("workflow-reader-dialog").open && $("workflow-reader-status").textContent.includes("아직 다운로드한"), "미다운로드 상태를 설명하고 읽기 진입 유지");
  ok(!$("workflow-reader-frame").getAttribute("src"), "없는 회차는 iframe을 열지 않음");
  $("workflow-reader-close").click();
  reader = { status: "ready", url: "javascript:alert(1)" };
  run('window.__reader = openEmbeddedReader("위험 URL","naver");'); await w.__reader;
  ok(!$("workflow-reader-frame").getAttribute("src") && !$("workflow-reader-external").getAttribute("href"), "실행 가능한 URL 스킴은 iframe과 외부 링크에 넣지 않음");
  reader = { status: "ready", url: "http://viewer/reader.html?series=book" };
  run('window.__reader = openEmbeddedReader("작품","naver");'); await w.__reader;
  const frame = $("workflow-reader-frame"), url = new URL(frame.src);
  ok(url.searchParams.get("embed") === "1" && url.searchParams.get("parent_origin") === "http://localhost", "관리기 origin을 명시한 내장 리더 URL");
  ok($("workflow-reader-external").href === reader.url, "별도 뷰어 링크는 원본 읽기 주소 유지");
  const readerStyle = w.document.createElement("style");
  readerStyle.textContent = require("fs").readFileSync(require("path").join(__dirname, "../../app/static/style.css"), "utf8");
  w.document.head.appendChild(readerStyle);
  const externalStyle = w.getComputedStyle($("workflow-reader-external"));
  const luminance = (color) => color.match(/[\d.]+/g).slice(0, 3).map(Number).map((value) => { const s = value / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
  const foreground = luminance(externalStyle.color), background = luminance(externalStyle.backgroundColor);
  ok((Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05) >= 4.5, "내장 리더의 별도 뷰어 링크가 배경 대비 4.5:1 이상으로 읽힘");
  const message = (origin, source, type) => w.dispatchEvent(new w.MessageEvent("message", { origin, source, data: { source: "webtoon-reader", version: 1, type } }));
  message("http://evil", frame.contentWindow, "close");
  message("http://viewer", w, "close");
  ok($("workflow-reader-dialog").open, "다른 origin과 다른 iframe의 close 메시지를 무시");
  message("http://viewer", frame.contentWindow, "ready");
  ok($("workflow-reader-status").textContent === "", "정확한 뷰어의 ready 응답만 처리");
  $("workflow-reader-close").click();
  ok(!!frame.getAttribute("src") && $("workflow-reader-close").disabled, "닫을 때 읽음 위치 저장 응답을 기다림");
  message("http://viewer", frame.contentWindow, "close");
  ok(!$("workflow-reader-dialog").open && !frame.getAttribute("src"), "저장 후 close 응답이 오면 iframe 제거");
  run('window.__reader = openEmbeddedReader("저장 실패 작품","naver");'); await w.__reader;
  $("workflow-reader-close").click();
  w.dispatchEvent(new w.MessageEvent("message", { origin: "http://viewer", source: frame.contentWindow, data: { source: "webtoon-reader", version: 1, type: "error", code: "progress_save_failed", message: "읽은 위치 저장 실패" } }));
  ok($("workflow-reader-dialog").open && !!frame.getAttribute("src") && $("workflow-reader-close").textContent.includes("다시 저장"), "저장 실패 시 리더를 유지하고 재시도 안내");
  $("workflow-reader-close").click();
  await waitFor(() => $("workflow-reader-status").textContent.includes("저장 응답"), "저장 응답 시간 초과", { timeout: 2500 });
  ok($("workflow-reader-dialog").open && !!frame.getAttribute("src"), "저장 응답이 늦어도 자동으로 닫지 않음");
  w.__confirm = false; $("workflow-reader-force-close").click();
  ok($("workflow-reader-dialog").open, "저장하지 않고 닫기 확인을 취소하면 유지");
  w.__confirm = true; $("workflow-reader-force-close").click();
  ok(!$("workflow-reader-dialog").open && !frame.getAttribute("src"), "사용자가 명시적으로 승인한 경우에만 저장 없이 닫기");
  done();
})();
