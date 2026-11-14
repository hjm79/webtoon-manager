const {makeApp,idle,waitFor,ok,done}=require('./harness');
(async()=>{
  let finishSave,saveFails=false;
  const {w,run,calls}=makeApp({
    '/api/authors/search':(opts,url)=>[{author_id:url.includes('A')?'1':'2',author_name:url.includes('A')?'A':'B',sample_title:'대표 작품'}],
    '/api/watched-authors':()=>new Promise((resolve,reject)=>{finishSave=()=>saveFails?reject(Error('이전 저장 실패')):resolve({});}),
    '/api/authors/interested':[], '/api/authors/candidates':[]
  });
  await idle();const $=id=>w.document.getElementById(id);
  async function overlapping(closeByButton){
    run('searchAndRegisterAuthor("A");');await idle();
    $('registry-author-results').querySelector('button').click();
    await waitFor(()=>finishSave,'저장 요청 시작');
    ok(!$('registry-author-close').disabled && /요청은 계속/.test($('registry-author-status').textContent),'저장 중 닫기 가능하며 요청이 계속됨을 안내');
    if(closeByButton)$('registry-author-close').click();
    else $('registry-author-dialog').dispatchEvent(new w.Event('cancel',{cancelable:true}));
    run('searchAndRegisterAuthor("B");');
    await waitFor(()=>$('registry-author-results').textContent.includes('ID 2'),'새 작가 선택창');
    const oldText=$('registry-author-status').textContent;
    finishSave();await idle();finishSave=null;
    ok($('registry-author-dialog').open && $('registry-author-title').textContent.includes('B'),'이전 저장 응답이 새 선택창을 닫지 않음');
    ok($('registry-author-status').textContent===oldText && !$('registry-author-results').querySelector('button').disabled,'이전 응답이 새 상태·버튼을 바꾸지 않음');
    $('registry-author-close').click();
  }
  await overlapping(false);
  saveFails=true;await overlapping(true);
  // A failure in the current chooser remains visible and can be retried.
  run('searchAndRegisterAuthor("A");');await idle();
  const button=$('registry-author-results').querySelector('button');button.click();
  await waitFor(()=>finishSave,'현재 저장 시작');finishSave();await idle();finishSave=null;
  ok($('registry-author-dialog').open && $('registry-author-status').textContent.includes('이전 저장 실패') && !button.disabled,'현재 저장 실패는 선택창에 표시하고 재시도 허용');
  saveFails=false;button.click();await waitFor(()=>finishSave,'재시도 시작');finishSave();await idle();
  ok(!$('registry-author-dialog').open,'현재 요청이 성공하면 정상적으로 닫음');
  ok(calls.filter(c=>c.url==='/api/watched-authors').length===4,'각 클릭은 한 번만 등록 요청');
  done();
})();
