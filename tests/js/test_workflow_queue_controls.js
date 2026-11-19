const { makeApp, idle, ok, done } = require("./harness");

(async () => {
  const queued = [
    { id: "a", platform: "naver", title_id: "101", title: "실행 작품", state: "running", rest_until: "2026-11-14T01:00:00Z" },
    { id: "b", platform: "kakao", title_id: "102", title: "다음 작품", state: "queued" },
    { id: "c", platform: "naver", title_id: "103", title: "마지막 작품", state: "queued" },
  ];
  let state = { download_busy: true, queue: queued, subscriptions: queued.map((row) => ({ ...row, status: row.state, scope: { mode: "all" } })) };
  const cancelled = [];
  const retried = [];
  const routes = {
    "/api/naver-list": [],
    "/api/workflow/status": () => state,
    "/api/workflow/queue": (opts) => {
      retried.push(JSON.parse(opts.body));
      state = { ...state, queue: state.queue.map((row) => row.id === "failed" ? { ...row, state: "queued" } : row) };
      return { execution: "queued", queue: state.queue };
    },
    "/api/workflow/queue/cancel": (opts) => {
      const value = JSON.parse(opts.body); cancelled.push(value);
      state = { ...state, queue: state.queue.filter((row) => row.title_id !== value.title_id) };
      return { execution: "cancelled", queue: state.queue };
    },
    "/api/workflow/preview": { subscription: "active", available_count: 1, current_scope: { mode: "all" }, episodes: [{ episode_no: 1, subtitle: "1화", available: true, owned: false }] },
  };
  const { w, run, calls } = makeApp(routes); await idle();
  const $ = (id) => w.document.getElementById(id);
  const rows = [...$("workflow-queue").children];
  ok(rows.length === 2 && rows[0].textContent.includes("다음 작품") && rows[1].textContent.includes("마지막 작품"), "대기열은 대기 항목만 서버 FIFO 순서대로 표시");
  ok(!rows[0].querySelector("button").disabled && $("workflow-progress-detail").textContent.includes("실행 작품") && $("workflow-state").textContent.includes("휴식"), "실행·휴식 작업은 진행 상태로 표시하고 대기 작업만 취소 가능");
  ok([...$("workflow-subscriptions").querySelectorAll("[data-scope-title-id]")].every((button) => button.disabled && button.title.includes("완료")), "다운로드 중 범위 변경은 사유와 함께 사전 비활성화");
  rows[0].querySelector("button").click(); await idle();
  ok(cancelled.length === 1 && cancelled[0].platform === "kakao" && cancelled[0].title_id === "102", "선택한 대기 작품만 취소 API로 전달");
  ok($("workflow-queue").children.length === 1 && $("workflow-queue").children[0].textContent.includes("마지막 작품"), "취소 후 대기 순서를 다시 표시");
  ok(!calls.some((call) => /unsubscribe|\/scope$|\/subscribe$/.test(call.url)), "대기 취소는 구독·범위 변경 요청을 하지 않음");

  state = { ...state, queue: [
    { id: "failed", platform: "naver", title_id: "901", title: "저장 실패", state: "failed", reason: "ZIP 저장 실패" },
    { id: "retry", platform: "kakao", title_id: "902", title: "자동 대기", state: "retry_download", retry_at: 1795100000 },
  ] };
  run('refreshWorkflowStatus()'); await idle();
  const failures = [...$("workflow-queue").children];
  ok(failures[0].textContent.includes("ZIP 저장 실패") && failures[1].textContent.includes("재시도 예정"), "실패 원인과 자동 재시도 시각을 표시");
  [...failures[0].querySelectorAll("button")].find((b) => b.textContent === "지금 다시 시도").click(); await idle();
  ok(retried.length === 1 && retried[0].title_id === "901" && state.queue[0].id === "failed" && state.queue[0].state === "queued", "기존 항목과 범위 그대로 실패 항목 재시도");

  const previews = () => calls.filter((call) => call.url === "/api/workflow/preview").length;
  const before = previews();
  run('chooseSubscriptionScope({platform:"naver",title_id:"104",title:"별도 구독"},{edit:true});'); await idle();
  ok($("workflow-subscribe-dialog").open && $("workflow-scope-fields").disabled && $("workflow-subscribe-submit").disabled && $("workflow-subscribe-download").disabled, "다른 작업 실행 중 범위 창은 편집·저장이 잠김");
  ok($("workflow-subscribe-status").textContent.includes("다른 다운로드") && previews() === before, "범위를 불러와 편집하기 전에 최신 busy 상태를 확인하고 이유를 표시");
  $("workflow-subscribe-cancel").click();
  run('window.__scopeButton = makeWorkflowScopeButton({platform:"naver",title_id:"104",title:"별도 구독"}); document.getElementById("workflow-search-results").appendChild(window.__scopeButton);');
  ok(w.__scopeButton.disabled, "수집 목록 밖 검색 결과의 범위 버튼도 busy 동안 잠김");
  state = { queue: [], subscriptions: [], download_busy: false };
  run('renderWorkflowStatus({queue:[],subscriptions:[],download_busy:false});');
  ok(!w.__scopeButton.disabled, "폴링에서 다운로드 종료가 확인되면 기존 검색 결과 버튼도 다시 활성화");
  run('chooseSubscriptionScope({platform:"naver",title_id:"104",title:"별도 구독"},{edit:true});'); await idle();
  ok(!$("workflow-scope-fields").disabled && !$("workflow-subscribe-submit").disabled && previews() === before + 1, "다운로드 종료 후 다시 열면 기존 범위 편집 가능");
  state = { queue: [], subscriptions: [], download_busy: true };
  run('renderWorkflowStatus({queue:[],subscriptions:[],download_busy:true});');
  ok($("workflow-scope-fields").disabled && $("workflow-subscribe-submit").disabled && $("workflow-subscribe-error").textContent.includes("다시 열어"), "편집 도중 작업이 시작되어도 저장 전에 사유와 잠금 표시");
  $("workflow-subscribe-cancel").click();
  run('chooseSubscriptionScope({platform:"naver",title_id:"105",title:"새 구독"});'); await idle();
  ok(!$("workflow-subscribe-submit").disabled, "다른 다운로드 중 새 구독 추가 기능은 유지");
  $("workflow-subscribe-cancel").click();
  done();
})();
