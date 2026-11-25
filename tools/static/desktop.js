'use strict';
const $=id=>document.getElementById(id);
const labels=JSON.parse($('desktop-labels').textContent);
const pages={atlas:'/world',recognition:'/world',meetings:'/meetings',fleet:'/fleet',maker:'/world'};
const workspaces=new Map();
let activePage='diplomacy';
let lastChats=null, statusPending=false, chatRequest=0;
let saveCatalogue=[], savedChats=[], saveChoiceInitialized=false, pendingSavedChoice=null, saveFilterRestored=false;
const bridge=()=>window.pywebview?.api;
const trace=(event,page=activePage,details=null)=>{
  try { Promise.resolve(bridge()?.trace(event,page,details)).catch(()=>{}); } catch {}
};
function statusText(value) {
  if ($('service-status').textContent===value) return;
  $('service-status').textContent=value;
  trace('status_changed');
}

function serviceState(chip,state,reason) {
  const caption=labels.states[state]||labels.states.error;
  const explanation=labels.reasons[reason]||reason||caption;
  chip.dataset.state=state;chip.title=explanation;
  chip.setAttribute('aria-label',chip.dataset.service+': '+caption+'. '+explanation);
  const value=chip.querySelector('.service-value');
  if(value.textContent!==caption)value.textContent=caption;
}
async function refreshStatus(manual=false) {
  if(statusPending||document.hidden)return;
  const chips=[...document.querySelectorAll('[data-service]')];
  if(!bridge()) {for(const chip of chips)serviceState(chip,'unavailable',labels.error);return;}
  statusPending=true;
  if(manual)for(const chip of chips)serviceState(chip,'checking','');
  try {
    const result=await bridge().status();
    for(const chip of chips) {
      const data=result.services?.[chip.dataset.service];
      if(data&&Object.hasOwn(labels.states,data.state))serviceState(chip,data.state,data.reason);
      else serviceState(chip,'error','probe_failed');
    }
    statusText('');
  } catch {for(const chip of chips)serviceState(chip,'error','probe_failed');trace('request_error');}
  finally{statusPending=false;}
}

function selectAtlas(frame) {
  frame.contentWindow?.dispatchEvent(new frame.contentWindow.CustomEvent('eyes-workspace',{detail:activePage}));
}

function show(page) {
  if (!(page==='diplomacy'||Object.hasOwn(pages,page))) return;
  if (activePage!==page) trace('workspace_changed',page);
  activePage=page;
  for (const button of $('tabs').querySelectorAll('button')) {
    const selected=button.dataset.page===page||(button.dataset.page==='recognition'&&page==='atlas');
    button.classList.toggle('active',selected);button.setAttribute('aria-pressed',String(selected));
  }
  $('recognition-modes').hidden=!['atlas','recognition'].includes(page);
  for(const button of $('recognition-modes').querySelectorAll('button')) {
    button.classList.toggle('active',button.dataset.page===page);
    button.setAttribute('aria-pressed',String(button.dataset.page===page));
  }
  $('workspaces').hidden=page==='diplomacy';
  $('chat-list').hidden=page!=='diplomacy';
  $('save-browser').hidden=page!=='diplomacy';
  for (const [path,frame] of workspaces) frame.hidden=path!==pages[page];
  if (page==='diplomacy') {refreshChats();return;}
  const path=pages[page];
  let frame=workspaces.get(path);
  if (frame) {if(path==='/world'&&frame.dataset.loaded)selectAtlas(frame);return;}
  frame=document.createElement('iframe');
  frame.title=$('workspaces').dataset.title;
  frame.setAttribute('sandbox','allow-same-origin allow-scripts allow-modals allow-forms allow-downloads');
  frame.addEventListener('load',()=>{
    frame.dataset.loaded='true';
    trace('workspace_loaded',path==='/world'?'atlas':page);
    if(path==='/world'&&!frame.hidden)selectAtlas(frame);
    frame.contentWindow?.addEventListener('error',()=>trace('workspace_error',page));
  });
  frame.src=path;
  workspaces.set(path,frame);
  $('workspaces').append(frame);
}

async function refreshChats() {
  const request=++chatRequest;
  try {
    const response=await fetch('/chats',{cache:'no-store'});
    if (!response.ok) throw Error();
    const result=await response.json();
    if (activePage!=='diplomacy'||request!==chatRequest) return;
    savedChats=result.chats;
    rebuildSaveOptions();
    const chatChoice=$('save-chat-choice'), signature=JSON.stringify(savedChats.map(chat=>[chat.chat_id,chat.display_title,chat.save_label]));
    if(chatChoice.dataset.signature!==signature){
      const selected=chatChoice.value;chatChoice.replaceChildren();
      for(const chat of savedChats)chatChoice.add(new Option(chat.display_title+' · '+chat.save_label,chat.chat_id));
      if(savedChats.some(chat=>chat.chat_id===selected))chatChoice.value=selected;
      chatChoice.dataset.signature=signature;
    }
    const chosenSave=$('save-choice').value;
    const visible=result.chats.filter(chat=>!chosenSave || (chosenSave==='unlinked'?!chat.save_scope_id:chat.save_scope_id===chosenSave));
    const view=JSON.stringify([chosenSave,visible.map(chat=>[chat.chat_id,chat.display_title,chat.save_label,chat.messages.length,chat.unread_count,chat.messages.slice(-3)])]);
    if(view===lastChats) return;
    lastChats=view;
    const focused=document.activeElement?.dataset.chatId, scroll=$('chat-list').scrollTop;
    $('chat-list').replaceChildren();
    if (!visible.length) $('chat-list').textContent=labels.none;
    for (const chat of visible) {
      const card=document.createElement('article');
      const name=document.createElement('h2');
      name.textContent=chat.display_title||chat.participants.join(' · ');
      const save=document.createElement('p');save.className='save-reference';save.textContent=chat.save_label||labels.saves.unlinked;
      const meta=document.createElement('div');meta.className='chat-meta';
      meta.textContent=labels.messages+': '+chat.messages.length+' · '+labels.unread+': '+(Number.isInteger(chat.unread_count)?chat.unread_count:labels.unknown);
      const details=document.createElement('div');
      for(const message of chat.messages.slice(-3)) {
        const reply=document.createElement('div');reply.className='diplomatic-reply';
        const speaker=document.createElement('strong');speaker.textContent=chat.country_labels?.[message.speaker]||message.speaker;
        // Original neutral pennant, not an unverified historical national flag.
        const flag=document.createElement('i');flag.className='country-pennant';flag.setAttribute('aria-hidden','true');speaker.prepend(flag);
        const text=document.createElement('span');text.textContent=message.text;
        reply.append(speaker,text);details.append(reply);
      }
      const button=document.createElement('button');
      button.dataset.chatId=chat.chat_id;
      button.textContent=labels.open;
      button.onclick=async()=>{
        if (!bridge()) return;
        const result=await bridge().open_chat(chat.chat_id);
        statusText(result.error||labels.ready);
      };
      card.append(name,save,meta,details,button);
      if(chat.chat_mode!=='ai_to_ai') {
        const participants=document.createElement('button');participants.textContent=labels.group.title;
        participants.onclick=()=>openGroup(chat);card.append(participants);
      }
      if(chat.participants.length===2&&chat.chat_mode!=='ai_to_ai') {
        const actionButton=document.createElement('button');
        actionButton.textContent=labels.treaty_actions.text.button;
        actionButton.onclick=()=>openTreatyActions(chat.chat_id);
        card.append(actionButton);
      }
      $('chat-list').append(card);
    }
    if(focused) [...$('chat-list').querySelectorAll('button')].find(button=>button.dataset.chatId===focused)?.focus({preventScroll:true});
    $('chat-list').scrollTop=scroll;
    trace('chat_redrawn');
  } catch {
    if(activePage!=='diplomacy'||request!==chatRequest)return;
    trace('request_error');
    if(lastChats===null)$('chat-list').textContent=labels.error;
  }
}
let selectedChat=null, approvedPreview=null, previewVersion=0;
async function refreshTreatyPreview() {
  const version=++previewVersion;
  approvedPreview=null;
  $('treaty-propose').disabled=true;
  $('treaty-preview').textContent='';
  $('treaty-history').replaceChildren();
  $('treaty-status').textContent='';
  const action=$('treaty-action-select').value;
  try {
    const response=await fetch('/chats/'+selectedChat+'/game-actions?action='+encodeURIComponent(action));
    if(!response.ok)throw Error();
    const data=await response.json();
    if(version!==previewVersion)return;
    $('treaty-preview').textContent=data.preview;
    for(const row of data.history) {
      const item=document.createElement('li');
      item.textContent=labels.treaty_actions.names[row.action]+' · v'+row.version+' · '+labels.treaty_actions.text[row.status];
      $('treaty-history').append(item);
    }
    approvedPreview={preview:data.preview,action,request_id:data.request_id};
    $('treaty-propose').disabled=false;
  } catch {
    if(version===previewVersion)$('treaty-status').textContent=labels.error;
  }
}
function openTreatyActions(chat) {
  selectedChat=chat;
  $('treaty-actions').showModal();
  refreshTreatyPreview();
}
$('treaty-actions').addEventListener('close',()=>{++previewVersion;approvedPreview=null;});
$('treaty-action-select').onchange=refreshTreatyPreview;
$('treaty-propose').onclick=async()=>{
  if(!approvedPreview)return;
  const version=previewVersion, preview=approvedPreview;
  approvedPreview=null;
  $('treaty-propose').disabled=true;
  try {
    const response=await fetch('/chats/'+selectedChat+'/game-actions',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(preview)});
    if(!response.ok)throw Error();
    if(version===previewVersion)$('treaty-status').textContent=labels.treaty_actions.text.sent;
    refreshChats();
  } catch {
    if(version===previewVersion)$('treaty-status').textContent=labels.error;
  }
};
function navigationClick(event){const button=event.target.closest('button[data-page]');if(button)show(button.dataset.page);}
$('tabs').onclick=navigationClick;
$('recognition-modes').onclick=navigationClick;
$('service-refresh').onclick=()=>{refreshStatus(true);if(activePage==='diplomacy')show(activePage);};
$('service-stop').onclick=async()=>{
  if (bridge()&&confirm(labels.confirm_stop)) {
    const result=await bridge().stop();
    $('service-status').textContent=result.error||result.detail;
    await refreshStatus();
  }
};
$('language-choice').onchange=async event=>{
  const result=await fetch('/world/locale',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({language:event.target.value})});
  if (result.ok) location.replace('/desktop?page='+activePage);
};
window.eyesShowWorkspace=show;
function errorDetails(error,source='',line=0,column=0) {
  return {kind:error?.name||'Error',source:source.split('/').pop().split('?')[0],line,column};
}
window.addEventListener('error',event=>trace('script_error',activePage,
  errorDetails(event.error,event.filename,event.lineno,event.colno)));
window.addEventListener('unhandledrejection',event=>trace('script_error',activePage,errorDetails(event.reason)));
async function refreshShortcut(key=null) {
  if(!bridge())return;
  try {
    const result=await bridge().atlas_shortcut(key);
    if(result.key)$('atlas-shortcut').value=result.key;
    $('atlas-shortcut-status').textContent=result.error||'';
  } catch { $('atlas-shortcut-status').textContent=labels.error; }
}
$('atlas-shortcut').onchange=event=>refreshShortcut(event.target.value);
window.addEventListener('pywebviewready',()=>{trace('shell_loaded');refreshStatus();refreshShortcut();});
setInterval(refreshStatus,5000);
show(new URLSearchParams(location.search).get('page')||'diplomacy');

function rebuildSaveOptions(){
    const select=$('save-choice');let previous=select.value;
    if(!saveChoiceInitialized){try{pendingSavedChoice=localStorage.getItem('eyes-save-filter')||null;}catch{}saveChoiceInitialized=true;}
    previous=previous||pendingSavedChoice||'';
    const rows=new Map(saveCatalogue.map(row=>[row.save_id,row]));
    for(const chat of savedChats)if(chat.save_reference&&!rows.has(chat.save_scope_id))rows.set(chat.save_scope_id,chat.save_reference);
    const signature=JSON.stringify([...rows.values()].map(row=>[row.save_id,row.name,row.date,row.player]));
    if(select.dataset.signature!==signature){
      select.replaceChildren(new Option(labels.saves.all,''),new Option(labels.saves.unlinked,'unlinked'));
      for(const row of rows.values()){
        const option=new Option(row.name+(row.date?' — '+row.date:'')+(row.player?' · '+row.player:''),row.save_id);
        option.title=row.path||row.filename||row.name;select.add(option);
      }
      select.dataset.signature=signature;
    }
    if([...select.options].some(option=>option.value===previous)){select.value=previous;pendingSavedChoice=null;}
    $('save-associate').disabled=!saveCatalogue.some(row=>row.save_id===select.value)||!savedChats.length;
}
async function refreshSaves(){
  try{
    const response=await fetch('/saves?refresh=1',{cache:'no-store'});if(!response.ok)throw Error();
    const result=await response.json();saveCatalogue=result.saves;
    if(!saveFilterRestored){pendingSavedChoice=pendingSavedChoice||result.selected_filter||null;saveFilterRestored=true;}
    rebuildSaveOptions();
    $('save-state').textContent=(result.saves.length?String(result.saves.length):labels.saves.none)+(result.truncated?' · '+labels.saves.incomplete:'');
    $('save-state').title=(result.warnings||[]).join(' ');
    if(activePage==='diplomacy'){lastChats=null;refreshChats();}
  }catch{$('save-state').textContent=labels.error;}
}
$('save-choice').addEventListener('change',()=>{
  pendingSavedChoice=null;saveFilterRestored=true;
  try{bridge()?.save_history_filter?.($('save-choice').value).catch(()=>{});}catch{}
  try{localStorage.setItem('eyes-save-filter',$('save-choice').value);}catch{}
  rebuildSaveOptions();
  const row=saveCatalogue.find(row=>row.save_id===$('save-choice').value);
  $('save-state').textContent=row?(labels.saves.errors[row.reason]||row.filename):'';
  lastChats=null;refreshChats();
});
$('save-refresh').addEventListener('click',refreshSaves);
$('save-folder').addEventListener('click',async()=>{
 if(!bridge())return;
 const result=await bridge().choose_save_folder();
 if(result.error)statusText(result.error);else await refreshSaves();
});
refreshSaves();

$('save-associate').addEventListener('click',async()=>{
 const selected=$('save-choice').value, chat=$('save-chat-choice').value;
 if(!saveCatalogue.some(row=>row.save_id===selected)||!chat)return statusText(labels.saves.none);
 if(!bridge()||!window.confirm(labels.saves.confirm))return;
 const result=await bridge().save_chat_context(chat,selected,true);
 if(result.error)return statusText(result.error);
 lastChats=null;await refreshChats();
});

let groupSource=null, groupCountries=[], groupMembers=new Set(), groupPending=false;
function renderGroupCountries() {
  const search=$('group-search').value.trim().toLocaleLowerCase();
  $('group-members').replaceChildren();
  for(const tag of groupMembers) {
    const chip=document.createElement('button');chip.type='button';
    const name=groupCountries.find(row=>row.tag===tag)?.name||tag;
    chip.textContent=name+' ×';chip.setAttribute('aria-label',labels.group.remove+': '+name);
    chip.disabled=groupPending;chip.onclick=()=>{groupMembers.delete(tag);renderGroupCountries();};
    $('group-members').append(chip);
  }
  $('group-options').replaceChildren();
  for(const row of groupCountries.filter(row=>row.tag!==groupSource?.player_country&&row.name.toLocaleLowerCase().includes(search)).sort((a,b)=>Number(groupMembers.has(b.tag))-Number(groupMembers.has(a.tag))||a.name.localeCompare(b.name))) {
    const label=document.createElement('label'),input=document.createElement('input');
    input.type='checkbox';input.checked=groupMembers.has(row.tag);
    input.disabled=groupPending||(!input.checked&&groupMembers.size>=6);
    input.onchange=()=>{if(input.checked)groupMembers.add(row.tag);else groupMembers.delete(row.tag);renderGroupCountries();};
    label.append(input,document.createTextNode(row.name));$('group-options').append(label);
  }
  $('group-apply').disabled=groupPending||groupMembers.size<1||groupMembers.size>6;
  $('group-status').textContent=labels.group.count+': '+groupMembers.size+' / 6';
}
async function openGroup(chat) {
  groupSource=chat;groupMembers=new Set(chat.participants.filter(tag=>tag!==chat.player_country));
  $('group-new').checked=!chat.group_negotiation;$('group-search').value='';
  $('group-source').textContent=(chat.country_labels?.[chat.player_country]||chat.player_country)+' · '+chat.save_label;
  $('group-dialog').showModal();groupPending=true;renderGroupCountries();
  try {
    const response=await fetch('/diplomacy/countries');if(!response.ok)throw Error();
    groupCountries=(await response.json()).countries;
    $('group-source').textContent=(groupCountries.find(row=>row.tag===chat.player_country)?.name||chat.player_country)+' · '+chat.save_label;
  } catch {$('group-status').textContent=labels.group.error;}
  finally{groupPending=false;renderGroupCountries();}
}
$('group-search').oninput=renderGroupCountries;
$('group-cancel').onclick=()=>$('group-dialog').close();
$('group-dialog').addEventListener('cancel',event=>{if(groupPending)event.preventDefault();});
$('group-apply').onclick=async()=>{
  if(groupPending||!groupSource)return;
  groupPending=true;$('group-cancel').disabled=true;renderGroupCountries();
  try {
    const response=await fetch('/chats/'+groupSource.chat_id+'/group',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ai_countries:[...groupMembers],new_group:$('group-new').checked})});
    const chat=await response.json();if(!response.ok)throw Error(chat.detail||labels.group.error);
    $('group-dialog').close();lastChats=null;await refreshChats();
    if(bridge()){const result=await bridge().open_chat(chat.chat_id);statusText(result.error||labels.ready);}
  } catch(error){$('group-status').textContent=error.message||labels.group.error;}
  finally{groupPending=false;$('group-cancel').disabled=false;$('group-apply').disabled=false;}
};
