'use strict';
const $=id=>document.getElementById(id);
const labels=JSON.parse($('desktop-labels').textContent);
const pages={atlas:'/world',recognition:'/world',meetings:'/meetings',fleet:'/fleet',maker:'/world'};
const workspaces=new Map();
let activePage='diplomacy';
let lastChats=null, statusPending=false, chatRequest=0;
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
    const view=JSON.stringify(result.chats.map(chat=>[chat.chat_id,chat.participants,chat.messages.length,chat.unread_count,chat.messages.slice(-3)]));
    if(view===lastChats) return;
    lastChats=view;
    const focused=document.activeElement?.dataset.chatId, scroll=$('chat-list').scrollTop;
    $('chat-list').replaceChildren();
    if (!result.chats.length) $('chat-list').textContent=labels.none;
    for (const chat of result.chats) {
      const card=document.createElement('article');
      const name=document.createElement('h2');
      name.textContent=chat.participants.join(' · ');
      const meta=document.createElement('div');meta.className='chat-meta';
      meta.textContent=labels.messages+': '+chat.messages.length+' · '+labels.unread+': '+(Number.isInteger(chat.unread_count)?chat.unread_count:labels.unknown);
      const details=document.createElement('div');
      for(const message of chat.messages.slice(-3)) {
        const reply=document.createElement('div');reply.className='diplomatic-reply';
        const speaker=document.createElement('strong');speaker.textContent=message.speaker;
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
      card.append(name,meta,details,button);
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
