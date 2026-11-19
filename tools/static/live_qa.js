'use strict';
let liveQATimer;
let lastLiveQA;

function showLiveQA(result) {
  const text = labels.workspace;
  const signature = JSON.stringify(result);
  if (signature === lastLiveQA) return;
  lastLiveQA = signature;
  const errors = {qa_close_game:text.qa_live_close_game,qa_unsupported_version:text.qa_live_version,
    qa_static_validation_failed:text.qa_live_static_failed};
  $('live-qa-state').textContent = result.error ? (errors[result.error] || result.error) : text['qa_live_'+result.phase];
  const counts = result.counts ? Object.entries(result.counts).map(([status,count])=>status+' '+count).join(' · ') : '';
  const bridgeCount = result.bridge?.expected ? 'Bridge '+result.bridge.seen+'/'+result.bridge.expected : '';
  const playset = result.phase==='waiting'&&!result.playset_selected ? text.qa_live_playset : '';
  $('live-qa-report').textContent = [result.name,playset,counts,bridgeCount,result.report,...(result.errors||[])].filter(Boolean).join(' · ');
  $('live-qa-results').replaceChildren(...(result.rows||[]).map(row=>{
    const item=document.createElement('li');
    const facts = document.documentElement.lang==='ru'?(row.ru||row):row;
    item.textContent=[row.title,row.status,facts.before&&facts.expected?facts.before+' → '+facts.expected:null,
      facts.actual,row.reason==='-'?null:facts.reason].filter(Boolean).join(' · ');
    return item;
  }));
}

async function pollLiveQA() {
  if (!$('live-qa-dialog').open || !bridge()) return;
  try {showLiveQA(await bridge().game_qa_status());} catch {showLiveQA({phase:'error',error:labels.error});}
  if ($('live-qa-dialog').open) liveQATimer=setTimeout(pollLiveQA,1800);
}

$('live-qa-open').onclick=()=>{
  $('live-qa-dialog').showModal();
  clearTimeout(liveQATimer);
  pollLiveQA();
};
$('live-qa-dialog').addEventListener('close',()=>clearTimeout(liveQATimer));
$('live-qa-prepare').onclick=async()=>{
  if (!bridge() || !confirm(labels.workspace.qa_live_confirm)) return;
  clearTimeout(liveQATimer);
  $('live-qa-prepare').disabled=true;
  try {showLiveQA(await bridge().prepare_game_qa());}
  catch {showLiveQA({phase:'error',error:labels.error});}
  finally {
    $('live-qa-prepare').disabled=false;
    if ($('live-qa-dialog').open) liveQATimer=setTimeout(pollLiveQA,4500);
  }
};
