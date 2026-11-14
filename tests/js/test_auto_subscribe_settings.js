const { makeApp, idle, ok, done } = require("./harness");
(async () => {
  let enabled = false, authorTracking = true, failLoad = false, failSave = false;
  const posts = [], cron = { mode: "off", interval_minutes: 60, cron_times: [{ hour: 3, minute: 0, days: [] }] };
  const { w, run, calls } = makeApp({
    "/api/settings": { discovery_job: cron, download_job: [], archive_job: cron },
    "/api/settings/auto-subscribe-scope": { mode: "future", latest_count: null },
    "/api/settings/auto-subscribe": (opts) => {
      if (opts.method === "POST") {
        const body = JSON.parse(opts.body); posts.push(body);
        if (failSave) throw new Error("정책 저장 실패");
        enabled = body.enabled;
      } else if (failLoad) throw new Error("정책 조회 실패");
      return { enabled };
    },
    "/api/settings/author-auto-register": (opts) => {
      if (opts.method === "POST") authorTracking = JSON.parse(opts.body).enabled;
      return { enabled: authorTracking };
    },
  });
  await idle(); run("loadSettingsPage();"); await idle();
  const $ = (id) => w.document.getElementById(id);
  ok($("settings-section-automation").contains($("auto-subscribe-toggle")) && !$("auto-subscribe-toggle").checked && !$("auto-subscribe-toggle").disabled, "자동 구독은 자동화 메뉴의 별도 명시적 옵션이며 기본 꺼짐을 불러옴");
  ok($("author-auto-register-toggle").checked && !$("auto-subscribe-toggle").checked, "기존 작가 추적 자동 추가가 켜져 있어도 새 작품 자동 구독은 켜지지 않음");
  ok(posts.length === 0 && /직접 추가/.test($("auto-subscribe-save-result").textContent), "설정 조회는 정책을 저장하지 않고 직접 구독 상태를 안내");
  ok(/추적 목록만/.test($("settings-card-author").textContent) && /자동 구독을 켜지 않습니다/.test($("settings-card-author").textContent), "작가 추적 설정과 자동 구독의 차이를 안내");
  ok(/추적 목록/.test($("page-registry").textContent) && /별도로 켤/.test($("page-registry").textContent), "작가·태그 관리 화면도 자동 구독을 별도로 켜야 함을 설명");

  $("auto-subscribe-toggle").checked = true;
  $("auto-subscribe-toggle").dispatchEvent(new w.Event("change"));
  const loaded = calls.filter((c) => c.url === "/api/settings/auto-subscribe").length;
  run('activateSettingsSection("viewer"); activateSettingsSection("automation");'); await idle();
  ok(posts.length === 0 && $("auto-subscribe-toggle").checked && calls.filter((c) => c.url === "/api/settings/auto-subscribe").length === loaded, "선택과 메뉴 이동만으로 정책이 저장되거나 미저장 선택이 초기화되지 않음");
  $("btn-save-auto-subscribe").click(); await idle();
  ok(enabled && posts.at(-1).enabled === true && posts.at(-1).scope.mode === "future" && /저장했습니다/.test($("auto-subscribe-save-result").textContent), "명시적 저장으로만 자동 구독을 켬");
  ok(authorTracking && $("author-auto-register-toggle").checked, "자동 구독 저장은 작가 추적 설정을 바꾸지 않음");
  $("author-auto-register-toggle").checked = false; $("btn-save-author-auto-register").click(); await idle();
  ok(!authorTracking && enabled && $("auto-subscribe-toggle").checked, "작가 추적 자동 추가를 꺼도 별도 자동 구독 정책은 바뀌지 않음");
  $("auto-subscribe-toggle").checked = false; $("btn-save-auto-subscribe").click(); await idle();
  ok(!enabled && /꺼짐/.test($("auto-subscribe-save-result").textContent), "자동 구독 끄기도 명시적 저장과 결과로 확인");
  failSave = true; $("auto-subscribe-toggle").checked = true; $("btn-save-auto-subscribe").click(); await idle();
  ok(!enabled && $("auto-subscribe-toggle").checked && /아직 적용되지 않았/.test($("auto-subscribe-save-result").textContent), "정책 저장 실패 시 서버 값은 유지하고 미저장 선택·실패 안내를 남김");
  failSave = false; failLoad = true; run("loadAutoSubscribeSetting();"); await idle();
  ok($("auto-subscribe-toggle").disabled && $("btn-save-auto-subscribe").disabled && /조회 실패/.test($("auto-subscribe-save-result").textContent), "정책 조회 실패 시 알 수 없는 상태로 저장하지 못하도록 제한");

  const subscriptions = ["manual", "artist", "tag", "unknown", "__proto__"].map((source, index) => ({ platform: "naver", title_id: String(100 + index), title: `작품 ${source}`, added_source: source, status: "available", downloaded_count: 1, scope: { mode: "all" } }));
  run(`renderWorkflowStatus(${JSON.stringify({ subscriptions, queue: [], download_busy: false })});`);
  const badges = Array.from($("workflow-subscriptions").querySelectorAll(".subscription-source"));
  ok(badges.length === 2 && badges[0].dataset.addedSource === "artist" && badges[0].textContent === "작가 자동 구독" && badges[1].dataset.addedSource === "tag" && badges[1].textContent === "태그 자동 구독", "실제 작가·태그 자동 구독 출처만 배지로 표시");
  ok(!calls.some((c) => c.method === "POST" && /unsubscribe|\/subscribe$|\/queue$|\/scope$/.test(c.url)), "정책 편집·출처 표시는 기존 구독·대기열·다운로드 범위를 바꾸지 않음");
  done();
})();
