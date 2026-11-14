const {makeApp,idle,ok,done}=require('./harness');
(async()=>{
  const state={
    download_busy:true,
    progress:{state:'success',title:'완료된 이전 작업',total:2,completed:2},
    queue:[
      {platform:'naver',title_id:'1',title:'현재 실행 작품',state:'running'},
      {platform:'naver',title_id:'2',title:'다음 대기 작품',state:'queued'},
      {platform:'kakao',title_id:'3',title:'완료된 대기열 찌꺼기',state:'success'},
      {platform:'naver',title_id:'4',title:'마지막 대기 작품',state:'queued'},
      {platform:'kakao',title_id:'5',title:'실패한 이전 작업',state:'error'},
    ],
    subscriptions:[
      {platform:'naver',title_id:'1',title:'현재 실행 작품',status:'running',downloaded_count:3,scope:{mode:'all'}},
      {platform:'naver',title_id:'2',title:'다음 대기 작품',status:'queued',downloaded_count:10,queue_position:2,scope:{mode:'latest',latest_count:1}},
      {platform:'naver',title_id:'6',title:'이미 받은 구독 작품',status:'available',downloaded_count:200,scope:{mode:'all'}},
    ],
  };
  const {w,run,calls}=makeApp({'/api/workflow/status':state,'/api/naver-list':[]});await idle();
  const $=id=>w.document.getElementById(id);
  ok($('page-downloads').classList.contains('hidden')&&$('workflow-overview').classList.contains('hidden'),'기본 수집 화면에 다운로드 현황을 노출하지 않음');
  w.document.querySelector('[data-page="downloads"]').click();await idle();
  ok(!$('page-downloads').classList.contains('hidden')&&!$('workflow-overview').classList.contains('hidden'),'전용 다운로드 현황 탭에서만 패널 표시');
  const items=[...$('workflow-queue').querySelectorAll('li')];
  ok(items.length===2&&items[0].textContent.includes('다음 대기 작품')&&items[1].textContent.includes('마지막 대기 작품'),'실행·완료·실패 작업은 제외하고 실제 대기 순서만 표시');
  ok(!$('workflow-queue').textContent.includes('이미 받은')&&!$('workflow-queue').textContent.includes('현재 실행')&&!$('workflow-queue').textContent.includes('완료된'),'구독 작품과 완료·진행 내역이 대기열에 섞이지 않음');
  ok(items[0].textContent.includes('다음 대기 작품'),'이미 받은 파일이 있어도 새 다운로드 작업이 실제 대기 중이면 유지');
  ok($('workflow-progress-heading').textContent==='실행 중인 작업'&&$('workflow-progress-detail').textContent.includes('현재 실행 작품')&&!$('workflow-progress-detail').textContent.includes('완료된 이전 작업'),'현재 실행 작업을 지난 완료 결과와 구분');
  ok(!$('workflow-subscription-panel').open&&$('workflow-subscription-panel').contains($('workflow-subscriptions'))&&$('workflow-subscription-panel').querySelector('summary').textContent==='구독 작품 관리','전체 구독 목록은 별도 접힌 관리 영역에 유지');
  ok($('workflow-subscriptions').textContent.includes('이미 받은 구독 작품')&&$('workflow-subscriptions').textContent.includes('대기열 1번째'),'구독 관리 기능을 유지하고 실제 대기 순번을 표시');
  run('Object.keys(pageLoaders).filter(p=>p!=="downloads").forEach(p=>pageLoaders[p]=()=>{});');
  for(const button of w.document.querySelectorAll('.main-tab')){
    if(button.dataset.page==='downloads')continue;
    button.click();await idle();
    ok($('workflow-overview').classList.contains('hidden')&&$('page-downloads').classList.contains('hidden')&&!$('page-'+button.dataset.page).classList.contains('hidden'),button.textContent+': 다운로드 현황 없이 해당 화면만 표시');
  }
  w.document.querySelector('[data-page="downloads"]').click();await idle();
  $('workspace-library').click();await idle();
  ok($('workflow-overview').classList.contains('hidden'),'내 서재에도 다운로드 패널을 표시하지 않음');
  $('workspace-collection').click();await idle();
  ok(!$('page-downloads').classList.contains('hidden'),'수집으로 돌아오면 선택했던 다운로드 탭 복원');
  run('renderWorkflowStatus({queue:[],progress:{state:"success",title:"방금 완료한 작품",total:1,completed:1},subscriptions:[{title:"완료 작품",platform:"naver",title_id:"9",downloaded_count:1,status:"available",scope:{mode:"all"}}]});');
  ok(!$('workflow-queue').querySelector('li')&&$('workflow-queue').textContent.includes('대기 작업이 없습니다'),'대기 작업이 없으면 완료 작품 대신 빈 대기열 안내');
  ok($('workflow-progress-heading').textContent==='최근 실행 결과'&&$('workflow-progress-detail').textContent.includes('방금 완료한 작품'),'완료 상태는 최근 결과로 명시');
  ok(!calls.some(c=>c.method==='POST'),'화면 이동과 표시 정리는 구독·파일·대기열을 변경하지 않음');
  done();
})();
