const { makeApp, idle, ok, done } = require('./harness');
(async () => {
  let health = { saved: true, enabled: true, state: 'reauth_required', message: '카카오페이지 재인증이 필요합니다.', days_left: 28, checked_at: '2026-11-15T00:00:00Z' };
  const queue = [{ id: 'preserved', platform: 'kakao', title_id: '990091', title: '원래 선택한 작품', state: 'blocked_auth', reason: health.message }];
  const { w, run, calls } = makeApp({
    '/api/workflow/status': () => ({ queue, subscriptions: [], download_busy: false, kakao_auth: health }),
    '/api/settings/kakao-page-login': () => health,
  });
  await idle();
  const $ = (id) => w.document.getElementById(id);
  ok(!$('kakao-auth-banner').classList.contains('hidden'), '남은 일수가 있어도 재인증 경고 표시');
  ok($('kakao-auth-banner-text').textContent.includes('다운로드 1개 대기'), '보존 중인 대기 작품 수 안내');
  ok($('workflow-queue').textContent.includes('원래 선택한 작품') && $('workflow-queue').textContent.includes('재인증 대기'), '보류된 작품이 대기열에 남음');
  ok(!$('workflow-queue').querySelector('button').disabled, '인증 대기도 기존 파일을 유지하며 취소 가능');
  $('kakao-auth-settings').click(); await idle();
  ok(!$('page-settings').classList.contains('hidden') && !$('settings-section-kakao').classList.contains('hidden'), '설정 버튼이 카카오 인증 메뉴로 이동');
  ok($('kp-cookie-status').textContent.includes('재인증') && $('kp-cookie-status').textContent.includes('마지막 확인'), '설정에서 실제 상태와 확인 시각 표시');
  health = { ...health, state: 'unknown', message: '연결 상태를 확인하지 못했습니다.' };
  queue[0].state = 'retry_wait';
  run('refreshWorkflowStatus()'); await idle();
  ok($('kakao-auth-banner-text').textContent.includes('연결 상태') && $('workflow-queue').textContent.includes('재시도 대기'), '연결 오류는 만료와 구분');
  health = { ...health, state: 'reauth_required', message: '카카오페이지 재인증이 필요합니다.', auto_refresh: { state: 'unknown' } };
  run('refreshWorkflowStatus()'); await idle();
  ok($('kakao-auth-banner-text').textContent.includes('자동 갱신 재시도 대기') && !$('kakao-auth-banner-text').textContent.includes('재인증이 필요'), '이전 프로필 거절 이후 일시 갱신 실패는 재시도 안내');
  health = { ...health, state: 'healthy', message: '로그인 정상' }; queue.length = 0;
  run('refreshWorkflowStatus()'); await idle();
  ok($('kakao-auth-banner').classList.contains('hidden'), '인증 복구 후 전역 경고 해제');
  health = { ...health, enabled: false, days_left: -1, auto_refresh: { state: 'reauth_required' } };
  run('loadKakaoPageCookieStatus()'); await idle();
  const statusText = () => $('kp-cookie-status').textContent;
  ok(statusText().includes('정기 세션 자동 갱신 꺼짐') && statusText().includes('만료 예상일 지남'), '정기 갱신 꺼짐 및 과거 예상일 표시');
  ok(!statusText().includes('새 쿠키 등록 필요'), '현재 인증이 정상이면 과거 갱신 거절로 재등록을 요구하지 않음');
  health = { ...health, enabled: true, auto_refresh: { state: 'unknown' } };
  run('loadKakaoPageCookieStatus()'); await idle();
  ok(statusText().includes('자동 갱신 일시 실패 · 재시도 예정') && statusText().includes('로그인 정상'), '일시 갱신 실패와 현재 로그인 상태를 분리');
  ok(!calls.some((call) => call.method === "POST" && /\/workflow\/(unsubscribe|subscribe|scope)$/.test(call.url)), '상태 확인은 구독이나 다운로드 범위를 변경하지 않음');
  done();
})();
