'use strict';
const actionText = labels.workspace;
let atlasActionContext = null;
let contextRequest = 0;
let acceptanceTimer = null;

function filterGameActions() {
  const choice = $('game-actions-choice'), previous=choice.value;
  const query = $('game-actions-query').value.trim().toLowerCase();
  const category = $('game-actions-category').value;
  const options = labels.game_catalog.filter(row =>
    (category==='all'||row.category===category) &&
    (row.name.toLowerCase().includes(query)||row.id.includes(query)));
  choice.replaceChildren(...options.map(row=>new Option(row.name,row.id)));
  if(options.some(row=>row.id===previous))choice.value=previous;
  renderGameAction();
}

function renderGameAction() {
  const row=labels.game_catalog.find(item=>item.id===$('game-actions-choice').value);
  const next=$('game-actions-next'), preview=$('game-actions-preview');
  $('game-actions-state').hidden=true;
  next.hidden=true;
  next.disabled=true;
  next.onclick=null;
  preview.textContent='';
  if(!row)return;
  if(!row.supported){preview.textContent=actionText.unsupported;return;}
  if(row.dlc)preview.textContent=row.dlc;
  const destination=row.id==='country_maker'?'maker':row.id==='recognition'?'recognition':
    row.category==='diplomatic'?'diplomacy':null;
  if(destination){
    next.hidden=false;
    next.textContent=actionText['open_'+destination];
    next.disabled=false;
    next.onclick=()=>{$('game-actions').close();show(destination);};
    return;
  }
  const data=atlasActionContext;
  if(!data?.states){
    preview.textContent=data?.error==='unverified_atlas_geometry'?actionText.unverified_atlas_geometry:actionText.missing_snapshot;
    return;
  }
  $('game-actions-state').hidden=false;
  const move=row.id==='move_capital';
  const infrastructure=row.id==='adjust_infrastructure';
  $('game-actions-capital-label').hidden=!move;
  $('game-actions-infrastructure').hidden=!infrastructure;
  const region=Number($('game-actions-target').value);
  const source=move?Number($('game-actions-capital').value):null;
  const fromLevel=infrastructure?$('game-actions-current-level').valueAsNumber:null;
  const toLevel=infrastructure?$('game-actions-level').valueAsNumber:null;
  const levelsValid=!infrastructure||([fromLevel,toLevel].every(v=>Number.isInteger(v)&&v>=0&&v<=5)
    && Math.abs(toLevel-fromLevel)===1);
  preview.textContent=[data.player,row.name,source?'#'+source+' →':null,region?'#'+region:null,
    infrastructure&&levelsValid?fromLevel+' → '+toLevel:null].filter(Boolean).join(' · ');
  next.hidden=false;
  next.textContent=actionText.output;
  next.disabled=!region||(move&&(!source||source===region))||!levelsValid;
  next.onclick=async()=>{
    if(!confirm(preview.textContent+'\n'+actionText.confirm_prepare))return;
    next.disabled=true;
    try{
      const response=await fetch('/world/state-operations',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:row.id,state:region,previous_capital:source,campaign:data.campaign,
          day:data.day,player:data.player,version:1,current_level:fromLevel,level:toLevel,confirmed:true})});
      if(!response.ok)throw Error();
      const result=await response.json();
      const file=new Blob([JSON.stringify(result.packet,null,2)],{type:'application/json'});
      const url=URL.createObjectURL(file), link=document.createElement('a');
      link.href=url;
      link.download='eyes-state-'+result.packet.agreement_id+'.json';
      link.click();
      setTimeout(()=>URL.revokeObjectURL(url),30000);
      await loadGameActionContext();
      preview.textContent=actionText.prepared;
    }catch{preview.textContent=labels.error;next.disabled=false;}
  };
}

async function loadGameActionContext() {
  const request=++contextRequest;
  try{
    const response=await fetch('/world/state-operations',{cache:'no-store'});
    const result=await response.json();
    if(request!==contextRequest)return;
    atlasActionContext=result;
    for(const id of ['game-actions-target','game-actions-capital']){
      const input=$(id), previous=input.value;
      input.replaceChildren(new Option(actionText.choose,''),...(result.states||[]).map(id=>new Option('#'+id,id)));
      if(result.states?.includes(Number(previous)))input.value=previous;
    }
    $('game-actions-history').replaceChildren(...(result.history||[]).map(row=>{
      const item=document.createElement('li');
      const name=labels.game_catalog.find(action=>action.id===row.action)?.name;
      item.textContent=[name,'#'+row.state,actionText[row.status]].join(' · ');
      return item;
    }));
  }catch{
    if(request!==contextRequest)return;
    atlasActionContext=null;
  }
  renderGameAction();
}

$('game-actions-open').onclick=()=>{
  atlasActionContext=null;
  $('game-actions').showModal();
  filterGameActions();
  loadGameActionContext();
};
$('game-actions-query').oninput=filterGameActions;
$('game-actions-category').onchange=filterGameActions;
for(const id of ['game-actions-choice','game-actions-target','game-actions-capital'])
  $(id).onchange=renderGameAction;
for(const id of ['game-actions-current-level','game-actions-level'])
  $(id).oninput=renderGameAction;

function displayAcceptance(result) {
  $('qa-state').textContent=actionText['qa_'+result.state]||actionText.qa_failed;
  const counters=Number.isInteger(result.tests)?result.tests+' / '+result.packages:'';
  $('qa-report').textContent=[counters,result.report||result.log].filter(Boolean).join(' · ');
  if(result.state==='running'&&$('qa-dialog').open){
    acceptanceTimer=setTimeout(async()=>{
      try{displayAcceptance(await bridge().acceptance_status());}catch{displayAcceptance({state:'failed'});}
    },700);
  }
}
$('qa-open').onclick=async()=>{
  $('qa-dialog').showModal();
  $('qa-state').textContent=actionText.qa_running;
  clearTimeout(acceptanceTimer);
  try{displayAcceptance(await bridge().start_acceptance());}catch{displayAcceptance({state:'failed'});}
};
$('qa-dialog').addEventListener('close',()=>clearTimeout(acceptanceTimer));
