'use strict';
const $=id=>document.getElementById(id);
const french=JSON.parse($('uiTranslations').textContent);
const say=(en,ru)=>language==='ru'?ru:language==='fr'?(french[en]||en):en;
let livePlayer=null, pendingCountry=null, packageBusy=false;
let language=document.documentElement.lang||'en',selected=new Set(),mapData,original,muted,ids,paths=new Map(),scale=1,tx=0,ty=0,drag=null,ready=false,rows=[],world=null,customFlag=null,flagVersion=0;
const base=$('base'),overlay=$('overlay'),ctx=base.getContext('2d'),oc=overlay.getContext('2d'),view=$('viewport');
const standalonePreview=new URLSearchParams(location.search).get('preview')==='more-provinces';
const mapQuery=standalonePreview?'?preview=more-provinces':'';
function region(s){const r=standalonePreview?null:world?.states[String(s)];return say('Region ','Область ')+s+(r?' · '+countryLabel(r.owner)+' / '+countryLabel(r.controller):'')}
function notice(){if(standalonePreview){$('notice').textContent=say('More Provinces preview · game ownership and territorial actions unavailable.','Предпросмотр More Provinces · игровое владение и территориальные действия недоступны.');return}if(snapshotError){$('notice').textContent=say('Unable to load snapshots. Refresh to retry.','Не удалось загрузить снимки. Нажмите «Обновить снимки».');return}if(world){$('notice').textContent=say('Snapshot: ','Снимок: ')+world.date+say(' · Current freshness not verified.',' · Актуальность сейчас не подтверждена.')+(geometryMatches()?say(' · Configured map geometry.',' · Настроенная геометрия карты.'):say(' · Region list differs from map.',' · Список областей отличается от карты.'));return}const mode=$('mode').value;$('notice').textContent=mode==='political'?say('Political data unavailable · showing installed geography, not campaign borders.','Политические данные недоступны · показана география, не границы партии.'):mode==='recognition'?say('Recognition layer unavailable · positions are not yet linked to map geometry.','Слой признания недоступен · позиции пока не связаны с геометрией карты.'):say('Installed geography · current ownership is not verified.','Установленная география · текущее владение не подтверждено.')}
function translate(){document.documentElement.lang=language;document.querySelectorAll('[data-en]').forEach(e=>e.textContent=say(e.dataset.en,e.dataset.ru));$('lang').value=language;notice();selection();fillCountries();updateMakerPlayer();if(!$('draft').hidden)preview();history()}
function selection(){const old=$('capital').value;$('selection').replaceChildren();$('capital').replaceChildren();for(const s of selected){let li=document.createElement('li'),label=document.createElement('span'),button=document.createElement('button');label.textContent=region(s);button.textContent='×';button.setAttribute('aria-label',say('Remove ','Удалить ')+region(s));button.onclick=()=>{selected.delete(s);selection();highlight()};li.append(label,button);$('selection').append(li);$('capital').add(new Option(region(s),s))}if(selected.has(Number(old)))$('capital').value=old;if(!selected.size){let li=document.createElement('li');li.textContent=say('Click regions on the map.','Выберите области на карте.');$('selection').append(li)}$('preview').disabled=!selected.size||!$('name').value.trim()||![...selected].every(eligibleRegion);if(!$('draft').hidden)preview();updateCreateButton()}
function preview(){if(!selected.size){$('draft').hidden=true;return}$('draft').hidden=false;$('draftName').textContent=$('name').value.trim()||say('Unnamed state','Государство без названия');$('draftInfo').textContent=say('Capital: ','Столица: ')+region($('capital').value)+' · '+$('status').selectedOptions[0].textContent+' · '+say('Regions: ','Области: ')+[...selected].join(', ')+($('countryTag').value?' · '+$('countryTag').value:'');const colors=flagPalettes[$('flag').value]||flagPalettes.blue;
$('flagPreview').style.background=customFlag?'center / contain no-repeat url("'+customFlag+'")':
  'linear-gradient('+colors[0]+' 0 50%,'+colors[1]+' 50% 100%)';
$('draftInfo').textContent+=' · '+say('Planned cores: ','Планируемые cores: ')+[...selected].join(', ');
$('draftInfo').textContent+=' · '+say('Map colour','Цвет на карте')+' '+$('countryHex').value;
$('draft').style.borderInlineStart='4px solid '+$('countryColor').value;
if($('initialIdeology').value)$('draftInfo').textContent+=' · '+$('initialIdeology').selectedOptions[0].textContent;
}
function transform(){ $('surface').style.transform=`translate(${tx}px,${ty}px) scale(${scale})`;$('scale').textContent=Math.round(scale*100)+'%'}
let mapViewport=null;
function fit(){if(!ready||!view.clientWidth||!view.clientHeight)return;mapViewport=[view.clientWidth,view.clientHeight];scale=Math.min(view.clientWidth/base.width,view.clientHeight/base.height);tx=(view.clientWidth-base.width*scale)/2;ty=(view.clientHeight-base.height*scale)/2;transform()}
function zoom(f,x=view.clientWidth/2,y=view.clientHeight/2){if(!ready)return;const next=Math.max(.08,Math.min(8,scale*f)),ratio=next/scale;tx=x-(x-tx)*ratio;ty=y-(y-ty)*ratio;scale=next;transform()}
function at(event){const r=view.getBoundingClientRect(),x=Math.floor((event.clientX-r.left-tx)/scale),y=Math.floor((event.clientY-r.top-ty)/scale);return x>=0&&y>=0&&x<base.width&&y<base.height?ids[y*base.width+x]:0}
function highlight(){if(!ready)return;oc.clearRect(0,0,base.width,base.height);oc.strokeStyle='#f6cf75';oc.lineWidth=2;oc.fillStyle='#e0ba5c30';for(const s of selected){const p=paths.get(s);if(p)oc.stroke(p)}}
function paint(){if(!ready)return;if(world&&geometryMatches()&&['political','controller','recognition'].includes($('mode').value)){political();highlight();notice();return;}ctx.putImageData($('mode').value==='provinces'?original:muted,0,0);highlight();notice()}
async function init(){try{const [metadata,image]=await Promise.all([fetch('/world/geography'+mapQuery).then(async r=>{const data=await r.json();if(!r.ok)throw data;return data;}),new Promise((resolve,reject)=>{const i=new Image();i.onload=()=>resolve(i);i.onerror=reject;i.src='/world/map'+mapQuery})]);mapData=metadata;const full=document.createElement('canvas');full.width=image.naturalWidth;full.height=image.naturalHeight;const fc=full.getContext('2d',{willReadFrequently:true});fc.drawImage(image,0,0);const pixels=fc.getImageData(0,0,full.width,full.height).data;
// Sample exact source pixels, never interpolate province identifiers. Render once at half resolution.
base.width=overlay.width=Math.ceil(full.width/2);base.height=overlay.height=Math.ceil(full.height/2);$('surface').style.width=base.width+'px';$('surface').style.height=base.height+'px';original=ctx.createImageData(base.width,base.height);muted=ctx.createImageData(base.width,base.height);ids=new Uint32Array(base.width*base.height);
for(let y=0;y<base.height;y++)for(let x=0;x<base.width;x++){const j=(y*2*full.width+x*2)*4,k=(y*base.width+x)*4,r=pixels[j],g=pixels[j+1],b=pixels[j+2],s=metadata.colors[String((r<<16)|(g<<8)|b)]||0;ids[k/4]=s;original.data.set([r,g,b,255],k);const v=s?((s*37)%24):0;muted.data.set(s?[65+v,82+v,84+v,255]:[19,43,58,255],k)}
for(let y=0;y<base.height;y++)for(let x=0;x<base.width;x++){const s=ids[y*base.width+x];if(!s)continue;let p=paths.get(s);if(!p){p=new Path2D();paths.set(s,p)}if(x===0||ids[y*base.width+x-1]!==s){p.moveTo(x,y);p.lineTo(x,y+1)}if(y===0||ids[(y-1)*base.width+x]!==s){p.moveTo(x,y);p.lineTo(x+1,y)}if(x===base.width-1||ids[y*base.width+x+1]!==s){p.moveTo(x+1,y);p.lineTo(x+1,y+1)}if(y===base.height-1||ids[(y+1)*base.width+x]!==s){p.moveTo(x,y+1);p.lineTo(x+1,y+1)}}
ctx.putImageData(muted,0,0);ctx.strokeStyle='#182c36';ctx.lineWidth=.5;for(const p of paths.values())ctx.stroke(p);muted=ctx.getImageData(0,0,base.width,base.height);full.width=full.height=0;ready=true;$('loading').hidden=true;fillCountries();fit();paint()}catch(error){if(error?.error==='duplicate_atlas_state'){
 $('loading').textContent=say('The playset defines the same state twice.','В наборе модов одна область определена дважды.')+' '+error.state+' · '+error.files.join(' / ');
 const link=document.createElement('a');link.href='/world?preview=more-provinces';
 link.textContent=say('Open More Provinces preview','Открыть предпросмотр More Provinces');
 $('loading').append(document.createElement('br'),link);return;
 }$('loading').textContent=say('Map unavailable. Check Flask and installed map files, then reload.','Карта недоступна. Проверьте Flask и файлы карты, затем обновите страницу.')}}
function history(){const box=$('history');box.replaceChildren();const filtered=rows.filter(r=>r.kind==='recognition').filter(r=>!$('observer').value.trim()||r.data.observer===$('observer').value.trim());if(!filtered.length){box.textContent=say('No verified position is known.','Достоверная позиция неизвестна.');return}const legal={recognized:say('Recognized','Признано'),not_recognized:say('Not recognized','Не признано'),unknown:say('Unknown','Неизвестно')};for(const r of filtered){const d=document.createElement('div');d.className='record';const v=r.data;d.textContent=[r.date,v.observer,v.subject,legal[v.legal]||legal.unknown,say('De facto: ','Фактическое принятие: ')+(v.de_facto===true?say('yes','да'):v.de_facto===false?say('no','нет'):legal.unknown),say('Protest: ','Протест: ')+(v.protest===true?say('yes','да'):v.protest===false?say('no','нет'):legal.unknown)].join(' · ');box.append(d)}}
$('lang').onchange=()=>{language=$('lang').value;fetch('/world/locale',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({language})}).catch(()=>{});translate()};$('mode').onchange=()=>{notice();paint()};$('clear').onclick=()=>{selected.clear();selection();highlight()};$('preview').onclick=reviewCountry;for(const id of ['name','capital','status','flag'])$(id).addEventListener('input',()=>{selection()});$('in').onclick=()=>zoom(1.3);$('out').onclick=()=>zoom(1/1.3);$('fit').onclick=fit;
view.addEventListener('wheel',e=>{e.preventDefault();const r=view.getBoundingClientRect();zoom(Math.exp(-e.deltaY*.001),e.clientX-r.left,e.clientY-r.top)},{passive:false});view.onpointerdown=e=>{if(e.button!==0)return;drag={x:e.clientX,y:e.clientY,tx,ty,moved:false};view.setPointerCapture(e.pointerId)};view.onpointermove=e=>{if(!ready)return;if(drag){const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.hypot(dx,dy)>4)drag.moved=true;if(drag.moved){tx=drag.tx+dx;ty=drag.ty+dy;transform()}}const s=at(e),tip=$('tooltip');tip.hidden=!s||!!drag;if(s){tip.textContent=region(s);const r=view.getBoundingClientRect();tip.style.left=Math.min(e.clientX-r.left+12,view.clientWidth-140)+'px';tip.style.top=Math.max(4,e.clientY-r.top-32)+'px'}};view.onpointerup=e=>{if(drag&&!drag.moved&&ready){const s=at(e);if(s&&(selected.has(s)||eligibleRegion(s))){selected.has(s)?selected.delete(s):selected.add(s);selection();highlight()}}drag=null};view.onpointercancel=()=>drag=null;view.onpointerleave=()=>$('tooltip').hidden=true;
for(const tab of ['maker','atlas'])$(tab+'Tab').onclick=()=>{for(const t of ['maker','atlas']){$(t).hidden=t!==tab;$(t+'Tab').classList.toggle('active',t===tab)}};
let campaignRequest=0, loadedCampaign='', loadedHeadDate='', loadedHeadId='';
let snapshotError=false;
function clearSnapshots() {
 rows=[];world=null;selected.clear();loadedCampaign='';loadedHeadDate='';loadedHeadId='';
 $('snapshotDate').replaceChildren();
 selection();fillCountries();paint();showChanges();history();renderPassports();
}
async function loadSnapshots() {
 const requestId=++campaignRequest;
 const campaign=$('campaign').value;
 const sameCampaign=campaign===loadedCampaign;
 const viewed=$('snapshotDate').value;
 const followLatest=!sameCampaign||!viewed||viewed===loadedHeadId;
 snapshotError=false;
 if(!sameCampaign)clearSnapshots();
 if(!campaign)return;
 try {
  const response=await fetch('/world/records?campaign='+encodeURIComponent(campaign),{cache:'no-store'});
  if(!response.ok)throw Error();
  const data=await response.json();
  if(requestId!==campaignRequest||campaign!==$('campaign').value)return;
  rows=data.records;
  const snapshots=rows.filter(row=>row.kind==='world_snapshot').sort((a,b)=>a.data.day-b.data.day);
  const head=snapshots.at(-1);
  loadedCampaign=campaign;
  loadedHeadDate=head?.date||'';
  loadedHeadId=head?.id||'';
  $('snapshotDate').replaceChildren(...snapshots.map(row=>new Option(row.date,row.id)));
  if(head){
   $('snapshotDate').value=!followLatest&&snapshots.some(row=>row.id===viewed)?viewed:head.id;
   if($('mode').value==='regions')$('mode').value='political';
   useSnapshot(sameCampaign);
  }else{
   world=null;selected.clear();selection();paint();
  }
  history();notice();renderPassports();
 }catch{
  if(requestId===campaignRequest){snapshotError=true;notice();}
 }
}
function updateOptions(control, options, emptyLabel) {
 const previous=control.value;
 if(!options.length&&emptyLabel)options=[new Option(emptyLabel,'')];
 if(options.length===control.options.length&&options.every((o,i)=>o.value===control.options[i].value&&o.text===control.options[i].text))return;
 control.replaceChildren(...options);
 if(options.some(option=>option.value===previous))control.value=previous;
}
let campaignRefresh=0, recognitionRevision=null;
async function refreshCampaigns(automatic=false) {
 const refreshId=++campaignRefresh;
 try {
  const response=await fetch('/world/campaigns',{cache:'no-store'});
  if(!response.ok)throw Error();
  const {campaigns}=await response.json();
  if(refreshId!==campaignRefresh)return;
  updateOptions($('campaign'),campaigns.map(c=>new Option(
   [c.player,c.date,c.id.slice(0,8)].filter(Boolean).join(' · '),c.id)));
  const chosen=campaigns.find(c=>c.id===$('campaign').value);
  if(!automatic||snapshotError||(chosen?.id||'')!==loadedCampaign||(chosen?.date||'')!==loadedHeadDate
      ||(chosen?.recognition_revision||null)!==recognitionRevision) {
   recognitionRevision=chosen?.recognition_revision||null;
   await loadSnapshots();
  }
 }catch{
  if(refreshId===campaignRefresh){snapshotError=true;notice();}
 }
}
$('campaign').onchange=loadSnapshots;
$('reloadSnapshots').onclick=()=>refreshCampaigns();
$('load').onclick=loadSnapshots;
new ResizeObserver(()=>{
 if(!ready||!view.clientWidth||!view.clientHeight)return;
 if(!mapViewport){fit();return;}
 const next=[view.clientWidth,view.clientHeight];
 if(next[0]===mapViewport[0]&&next[1]===mapViewport[1])return;
 tx+=(next[0]-mapViewport[0])/2;ty+=(next[1]-mapViewport[1])/2;
 mapViewport=next;transform();
}).observe(view);

function useSnapshot(keepSelection=false) {
 world=rows.find(row=>row.id===$('snapshotDate').value)?.data||null;
 if(!keepSelection)selected.clear();
 else for(const state of selected)if(!eligibleRegion(state))selected.delete(state);
 selection();fillCountries();paint();showChanges();updateMakerPlayer();
}
$('snapshotDate').onchange=()=>useSnapshot();

function countryLabel(tag) {
 const name=mapData?.country_names?.[language]?.[tag];
 return name ? name+' ('+tag+')' : tag;
}
function geometryMatches() {
 const mapIds=mapData?.state_ids;
 return !!world && mapData.geometry_verified===true && !!mapIds && mapIds.length===Object.keys(world.states).length &&
   mapIds.every(id=>Object.hasOwn(world.states,id));
}
function fillCountries() {
 const focus=$('countryFocus'), old=focus.value;
 focus.replaceChildren(new Option(say('All countries','Все страны'),''));
 for(const tag of (world?.countries||[])) focus.add(new Option(countryLabel(tag),tag));
 focus.value=(world?.countries||[]).includes(old)?old:'';
}
function showChanges() {
 const earlier=rows.filter(r=>r.kind==='world_snapshot'&&r.data.day<world?.day)
  .sort((a,b)=>b.data.day-a.data.day)[0]?.data;
 const element=$('boundaryChanges');
 if(!earlier){element.textContent='';return;}
 const changed=Object.entries(world.states).filter(([id,row])=>earlier.states[id]&&earlier.states[id].owner!==row.owner);
 element.textContent=say('Owner changes since ','Смена владельцев с даты ')+earlier.date+' · '+changed.length;
}
function recognitionColors() {
 const observer=($('observer').value.trim()||world.player||'').toUpperCase(), positions=new Map();
 if(!observer)return positions;
 for(const record of rows) {
  const data=record.data;
  if(record.kind!=='recognition'||data.observer!==observer||!Number.isInteger(data.day)||data.day>world.day)continue;
  const subject=/^(?:state:([1-9][0-9]*)|country:([A-Z][A-Z0-9]{2}))$/.exec(data.subject);
  if(!subject)continue;
  const id=subject[2]||Number(subject[1]), prior=positions.get(id)||{dates:{}};
  for(const axis of ['legal','de_facto','protest']){
   if(data[axis]!==undefined&&data[axis]!==null&&(prior.dates[axis]??-1)<=data.day){
    prior[axis]=data[axis];prior.dates[axis]=data.day;
   }
  }
  positions.set(id,prior);
 }
 return positions;
}
function political() {
 const image=ctx.createImageData(base.width,base.height);
 const axis=$('mode').value==='controller'?'controller':'owner';
 const isRecognition=$('mode').value==='recognition', positions=isRecognition?recognitionColors():null;
 const focus=$('countryFocus').value, palette=new Map();
 const countryColours=new Map(rows.filter(row=>row.kind==='country_creation'&&row.data.status==='applied'&&
   row.data.result_day<=world.day&&/^#[0-9a-f]{6}$/i.test(row.data.color||'')).map(({data})=>[
   data.new_tag,[1,3,5].map(offset=>parseInt(data.color.slice(offset,offset+2),16))
 ]));
 for(const [id,row] of Object.entries(world.states)){
  const tag=row[axis], position=positions?.get(Number(id))||positions?.get(tag);
  let color=[65,72,76];
  if(isRecognition){
   if(position?.protest===true)color=[177,137,66];
   else if(position?.legal==='recognized')color=[73,137,102];
   else if(position?.legal==='not_recognized')color=[151,72,70];
   else if(position?.de_facto===true)color=[65,117,155];
  }else if(!focus||focus===tag){
   let hash=2166136261;
   for(const char of tag)hash=Math.imul(hash^char.charCodeAt(0),16777619)>>>0;
   color=countryColours.get(tag)||[65+hash%100,65+(hash>>>8)%100,65+(hash>>>16)%100];
  }
  palette.set(Number(id),color);
 }
 for(let i=0;i<ids.length;i++){
  const color=ids[i]?palette.get(ids[i])||[46,56,62]:[19,43,58], offset=i*4;
  image.data[offset]=color[0];image.data[offset+1]=color[1];
  image.data[offset+2]=color[2];image.data[offset+3]=255;
 }
 ctx.putImageData(image,0,0);ctx.strokeStyle='#15232d';ctx.lineWidth=.5;
 for(const path of paths.values())ctx.stroke(path);
}
const flagPalettes={blue:['#294a69','#d5b575'],red:['#77353a','#eee5ca'],green:['#355f53','#c6d3d1']};
const shades=['#233b65','#732f3d','#32624b','#3b344d','#d3b577','#e8dfc5'];
for(const [first,a] of shades.entries()){
 for(const [second,b] of shades.entries()){
  if(first===second)continue;
  const key='palette-'+first+'-'+second;
  flagPalettes[key]=[a,b];
  const option=new Option(a+' / '+b,key);
  $('flag').add(option);
 }
}
$('countryFocus').onchange=paint;
$('observer').onchange=()=>{history();if($('mode').value==='recognition')paint()};
$('flag').onchange=()=>{flagVersion++;if(customFlag)URL.revokeObjectURL(customFlag);customFlag=null;$('flagUpload').value='';$('flagError').textContent='';if(!$('draft').hidden)preview()};
$('flagUpload').onchange=async()=>{
 const version=++flagVersion, file=$('flagUpload').files[0];
 if(!file)return;
 let url;
 try{
  if(file.size<33||file.size>2*1024*1024)throw Error();
  const header=new Uint8Array(await file.slice(0,33).arrayBuffer());
  if(![137,80,78,71,13,10,26,10].every((byte,i)=>header[i]===byte))throw Error();
  const data=new DataView(header.buffer);
  if(data.getUint32(8)!==13||data.getUint32(12)!==0x49484452)throw Error();
  const width=data.getUint32(16),height=data.getUint32(20);
  if(!width||!height||width>2048||height>2048)throw Error();
  url=URL.createObjectURL(new Blob([file],{type:'image/png'}));
  const image=new Image();image.src=url;await image.decode();
  if(version!==flagVersion){URL.revokeObjectURL(url);return;}
  if(customFlag)URL.revokeObjectURL(customFlag);
  customFlag=url;$('flagError').textContent='';
  if(!$('draft').hidden)preview();
 }catch{
  if(url)URL.revokeObjectURL(url);
  if(version===flagVersion)$('flagError').textContent=say('Choose a valid PNG up to 2 MB and 2048 × 2048.','Выберите корректный PNG до 2 МБ и 2048 × 2048.');
 }
};
function eligibleRegion(id) {
 const allocation=world?.states[String(id)];
 return !!world?.player && loadedCampaign===$('campaign').value && geometryMatches() &&
   allocation?.owner===world.player && allocation?.controller===world.player;
}
function canExecuteDraft() {
 return !!livePlayer?.can_prepare && !!world &&
   livePlayer.campaign===loadedCampaign && livePlayer.campaign===$('campaign').value &&
   livePlayer.player===world.player && livePlayer.day===world.day &&
   selected.size>0 && [...selected].every(eligibleRegion);
}
function updateMakerPlayer() {
 const label=$('makerPlayer');
 label.textContent=world?.player
  ?say('Planning for ','Проект для ')+countryLabel(world.player)+' · '+world.date
  :say('Select a campaign snapshot to choose regions.','Выберите снимок кампании для выбора областей.');
 if(livePlayer?.branch_pending){
  label.textContent+=' · '+say('Bind the current export session to this branch to receive new snapshots.',
    'Чтобы получать новые снимки, привяжите текущую сессию экспорта к этой ветке.');
 }
}
function reviewCountry() {
 if(!selected.size||![...selected].every(eligibleRegion))return;
 if(!$('countryHex').reportValidity()||!$('countryTag').reportValidity())return;
 preview();
 $('confirmCountryName').textContent=$('draftName').textContent;
 $('confirmCountryTerms').textContent=$('draftInfo').textContent;
 pendingCountry=canExecuteDraft() ? {name:$('name').value.trim(), states:[...selected], capital:Number($('capital').value),
   status:$('status').value, campaign:livePlayer.campaign, day:livePlayer.day,confirmed:true,
   country_tag:$('countryTag').value, color:$('countryHex').value, initial_ideology:$('initialIdeology').value||null,
   profiles:JSON.parse(JSON.stringify(countryProfiles))}:null;
 $('downloadCountry').disabled=!pendingCountry;
 $('confirmCountry').showModal();
}
async function refreshPlayer() {
 let next=null;
 try{
  const response=await fetch('/world/player',{cache:'no-store'});
  if(!response.ok)throw Error();
  next=await response.json();
 }catch{}
 if(JSON.stringify(next)===JSON.stringify(livePlayer))return;
 livePlayer=next;
 updateMakerPlayer();updateCreateButton();
}
function updateCreateButton() {
 const ready=canExecuteDraft()&&$('name').value.trim();
 $('createCountry').disabled=packageBusy||!ready;
 const label=$('createReason');
 if(livePlayer?.branch_pending){
  label.textContent=say('Bind the current export session to the selected branch first.','Сначала привяжите текущую сессию экспорта к выбранной ветке.');
 }else if(livePlayer?.reason==='game_process_changed'){
  label.textContent=say('HOI4 restarted. Export a snapshot from the loaded game, then bind the new session to this branch.',
    'HOI4 перезапущена. Получите снимок загруженной партии и привяжите новую сессию к этой ветке.');
 }else if(livePlayer?.reason==='native_log_changed_or_stale'){
  label.textContent=say('The previous export no longer matches the current game log. Request a new snapshot.',
    'Прежний экспорт больше не соответствует текущему журналу игры. Запросите новый снимок.');
 }else if(livePlayer?.reason==='game_session_unavailable'){
  label.textContent=say('Load the campaign in HOI4 and wait for an export.','Загрузите кампанию в HOI4 и дождитесь экспорта.');
 }else if(!livePlayer?.can_prepare||world?.day!==livePlayer.day||loadedCampaign!==livePlayer.campaign){
  label.textContent=say('You can select regions from this snapshot. Preparing a package requires a fresh export for this branch.',
    'Области можно выбирать по снимку. Для подготовки пакета нужен свежий экспорт этой ветки.');
 }else if(!ready){
  label.textContent=say('Enter a name and select regions owned and controlled by the player.','Введите название и выберите области, которыми игрок владеет и управляет.');
 }else{
  label.textContent=say('Prepare a test package, then confirm the action in HOI4.','Подготовьте тестовый пакет, затем подтвердите действие в HOI4.');
 }
}
function renderPassports() {
 const box=$('countryPassports');
 box.replaceChildren();
 for(const record of rows.filter(row=>row.kind==='country_creation')){
  const state=record.data, item=document.createElement('p');
  const labels={prepared:say('Prepared','Подготовлено'),partial:say('Partial result','Частичный результат'),
   blocked:say('Blocked','Заблокировано'),applied:say('Confirmed in HOI4','Подтверждено HOI4'),
   rolled_back:say('Territory changes reversed','Изменения территорий отменены'),
   rollback_failed:say('Rollback could not be verified','Возврат территорий не подтверждён')};
  const capitalLabel=state.status==='applied'&&state.executor_version>=3
   ?say('Verified capital ','Подтверждённая столица ')
   :say('Requested capital ','Выбранная столица ');
  item.textContent=[state.name, state.status==='applied'?state.new_tag:null, labels[state.status],
   capitalLabel+state.capital,
   state.country_status==='dependent'?say('Puppet','Марионетка'):say('Independent','Независимое'),
   say('Regions ','Области ')+state.states.join(', ')].filter(Boolean).join(' · ');
  box.append(item);
 }
}
async function packageFlag(url,palette) {
 let source=null;
 if(url){
  source=new Image();source.src=url;await source.decode();
 }
 const pixels={};
 for(const [name,width,height] of [['large',82,52],['medium',41,26],['small',10,7]]){
  const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
  const context=canvas.getContext('2d');
  context.fillStyle=palette[0];context.fillRect(0,0,width,height);
  if(source){
   const scale=Math.min(width/source.naturalWidth,height/source.naturalHeight);
   const w=source.naturalWidth*scale,h=source.naturalHeight*scale;
   context.drawImage(source,(width-w)/2,(height-h)/2,w,h);
  }else{
   context.fillStyle=palette[1];context.fillRect(0,Math.floor(height/2),width,height);
  }
  const rgba=context.getImageData(0,0,width,height).data;
  pixels[name]=btoa(String.fromCharCode(...rgba));
 }
 return pixels;
}
const ideologyLabels={
 democratic:['Democratic','Демократия'], fascism:['Fascism','Фашизм'],
 communism:['Communism','Коммунизм'], neutrality:['Non-Aligned','Нейтралитет']
};
const profileFields={
 given_name:['First name','Имя'],family_name:['Last name','Фамилия'],title:['Title','Должность'],
 country_name:['Name under this ideology','Название при этой идеологии'],party:['Party name','Название партии']
};
const designKey='eyes.countryMaker.design';
let savedDesign={};
try{savedDesign=JSON.parse(localStorage.getItem(designKey)||'{}')||{};}catch{}
const countryProfiles={};
const setLabel=(element,[en,ru])=>{element.dataset.en=en;element.dataset.ru=ru;element.textContent=say(en,ru);};
function saveDesign(){
 try{localStorage.setItem(designKey,JSON.stringify({
  tag:$('countryTag').value,color:$('countryColor').value,initial:$('initialIdeology').value,profiles:countryProfiles
 }));}catch{}
 if(!$('draft').hidden)preview();
}
function drawSavedPortrait(canvas,encoded){
 const pixels=Uint8ClampedArray.from(atob(encoded),char=>char.charCodeAt(0));
 canvas.getContext('2d').putImageData(new ImageData(pixels,156,210),0,0);
 canvas.hidden=false;
}
async function readPortrait(file,canvas){
 if(file.size>4*1024*1024)throw Error();
 const header=new Uint8Array(await file.slice(0,8).arrayBuffer());
 const png=[137,80,78,71,13,10,26,10].every((value,index)=>header[index]===value);
 const jpeg=header[0]===255&&header[1]===216&&header[2]===255;
 if(!png&&!jpeg)throw Error();
 const bitmap=await createImageBitmap(file);
 try{
  if(!bitmap.width||!bitmap.height||bitmap.width>4096||bitmap.height>4096)throw Error();
  const context=canvas.getContext('2d'), scale=Math.max(156/bitmap.width,210/bitmap.height);
  const width=bitmap.width*scale,height=bitmap.height*scale;
  context.fillStyle='#202832';context.fillRect(0,0,156,210);
  context.drawImage(bitmap,(156-width)/2,(210-height)/2,width,height);
  const rgba=context.getImageData(0,0,156,210).data, chunks=[];
  for(let offset=0;offset<rgba.length;offset+=8192)chunks.push(String.fromCharCode(...rgba.subarray(offset,offset+8192)));
  return btoa(chunks.join(''));
 }finally{bitmap.close();}
}
for(const [ideology,labels] of Object.entries(ideologyLabels)){
 const option=new Option('',ideology);setLabel(option,labels);$('initialIdeology').add(option);
 const panel=document.createElement('details'),heading=document.createElement('summary');
 panel.className='leader-profile';setLabel(heading,labels);panel.append(heading);
 const profile=countryProfiles[ideology]={...(savedDesign.profiles?.[ideology]||{})};
 for(const [field,fieldLabels] of Object.entries(profileFields)){
  const label=document.createElement('label'),input=document.createElement('input');
  input.id=ideology+'_'+field;label.htmlFor=input.id;setLabel(label,fieldLabels);
  input.maxLength=['country_name','party'].includes(field)?120:80;input.value=profile[field]||'';
  if(field==='title')input.setAttribute('list','leaderTitles');
  input.oninput=()=>{profile[field]=input.value;saveDesign();};
  panel.append(label,input);
 }
 const label=document.createElement('label'),upload=document.createElement('input'),canvas=document.createElement('canvas');
 const error=document.createElement('p'),remove=document.createElement('button');
 upload.type='file';upload.accept='image/png,image/jpeg';upload.id=ideology+'_portrait';label.htmlFor=upload.id;
 setLabel(label,['Leader portrait (PNG or JPG)','Портрет лидера (PNG или JPG)']);
 setLabel(remove,['Remove portrait','Удалить портрет']);
 canvas.width=156;canvas.height=210;canvas.hidden=true;canvas.className='portrait-preview';
 error.setAttribute('role','status');
 try{if(profile.portrait)drawSavedPortrait(canvas,profile.portrait);}catch{delete profile.portrait;}
 remove.hidden=!profile.portrait;
 let revision=0;
 upload.onchange=async()=>{
  if(!upload.files[0])return;
  const current=++revision;
  try{
   const scratch=document.createElement('canvas');scratch.width=156;scratch.height=210;
   const encoded=await readPortrait(upload.files[0],scratch);
   if(current!==revision)return;
   profile.portrait=encoded;drawSavedPortrait(canvas,encoded);
   remove.hidden=false;error.textContent='';saveDesign();
  }catch{
   if(current===revision)error.textContent=say('Choose a PNG or JPG up to 4 MB and 4096 pixels per side.',
    'Выберите PNG или JPG до 4 МБ и не более 4096 пикселей по каждой стороне.');
  }finally{if(current===revision)upload.value='';}
 };
 remove.onclick=()=>{revision++;upload.value='';delete profile.portrait;canvas.hidden=true;
  remove.hidden=true;error.textContent='';saveDesign();};
 panel.append(label,upload,error,canvas,remove);$('leaderProfiles').append(panel);
}
if(/^X[A-Z]{2}$/.test(savedDesign.tag||''))$('countryTag').value=savedDesign.tag;
$('countryTag').oninput=()=>{$('countryTag').value=$('countryTag').value.toUpperCase();saveDesign();};
if(/^#[0-9a-f]{6}$/i.test(savedDesign.color||''))$('countryColor').value=savedDesign.color;
if(Object.hasOwn(ideologyLabels,savedDesign.initial))$('initialIdeology').value=savedDesign.initial;
$('countryHex').value=$('countryColor').value.toUpperCase();
$('initialIdeology').onchange=saveDesign;
$('countryColor').oninput=()=>{$('countryHex').value=$('countryColor').value.toUpperCase();saveDesign();};
$('countryHex').oninput=()=>{
 if(/^#[0-9a-f]{6}$/i.test($('countryHex').value)){$('countryColor').value=$('countryHex').value;saveDesign();}
};
$('createCountry').onclick=reviewCountry;
$('downloadCountry').onclick=async()=>{
 if(!pendingCountry||packageBusy)return;
 const plan=pendingCountry, flag=customFlag, palette=flagPalettes[$('flag').value];
 packageBusy=true; $('downloadCountry').disabled=true; updateCreateButton();
 try{
  plan.flag_pixels=await packageFlag(flag,palette);
  const response=await fetch('/world/country-package',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(plan)});
  if(!response.ok)throw Error((await response.json()).error);
  const url=URL.createObjectURL(await response.blob());
  const link=document.createElement('a');
  link.href=url;link.download=response.headers.get('Content-Disposition')?.match(/filename="?([^";]+)"?/)?.[1]||'eyes_country.zip';
  link.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
  $('confirmCountry').close();await loadSnapshots();
 }catch(error){
  const errors={
   snapshot_state_roster_mismatch:['Regions in this export differ from the configured map.','Области в этом экспорте отличаются от настроенной карты.'],
   unverified_atlas_geometry:['The map has not been verified for country creation.','Карта для создания государства не подтверждена.'],
   invalid_requested_country_tag:['Choose a tag between XAA and XZZ.','Выберите тег от XAA до XZZ.'],
   country_tag_already_reserved:['This tag is already used or reserved. Choose another.','Этот тег уже используется или зарезервирован. Выберите другой.'],
   country_portrait_or_title_requires_leader_name:['Enter a leader name before adding a title or portrait.','Укажите имя лидера перед добавлением должности или портрета.'],
   invalid_country_profile_text:['A leader, country, party, or title field contains unsupported characters.','В имени, названии страны, партии или должности есть недопустимые символы.']
  };
  $('confirmCountryTerms').textContent=say(...(errors[error.message]||
   ['Package preparation failed. Refresh the snapshot and review the project.','Пакет не подготовлен. Обновите снимок и проверьте проект.']));
 }finally{packageBusy=false;pendingCountry=null;updateCreateButton();}
};
let pendingRefresh=0, binding=false;
async function refreshPendingSessions() {
 const requestId=++pendingRefresh;
 try{
  const response=await fetch('/world/pending-sessions',{cache:'no-store'});
  if(!response.ok)throw Error();
  const {sessions}=await response.json();
  if(requestId!==pendingRefresh)return;
  updateOptions($('pendingSession'),sessions.map(session=>new Option(
   session.player+' · '+session.date+' · '+session.count+' · '+session.id.slice(0,8),session.id)),
   say('No pending exports','Нет ожидающих экспортов'));
  $('newBranch').disabled=binding||!sessions.length;
  $('resumeBranch').disabled=binding||!sessions.length;
 }catch{
  $('branchStatus').textContent=say('Could not read pending exports.','Не удалось получить ожидающие экспорты.');
 }
}
async function bindBranch(campaign) {
 const session=$('pendingSession').value;
 if(binding||!session)return;
 const message=campaign
  ?say('Bind the selected exports to this save branch?','Привязать выбранные экспорты к этой ветке сохранения?')
  :say('Register a separate branch for the selected exports?','Зарегистрировать отдельную ветку для выбранных экспортов?');
 if(!window.confirm(message))return;
 binding=true;
 $('newBranch').disabled=$('resumeBranch').disabled=true;
 try{
  const response=await fetch('/world/bind-branch',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({campaign,session_id:session,confirmed:true})});
  const result=await response.json();
  if(!response.ok){
   const reason=result.error==='wrong_world_branch'
    ?say('The export belongs to a different campaign.','Экспорт относится к другой кампании.')
    :result.error==='snapshot_rewind_requires_separate_campaign'||result.error==='conflicting_same_day_snapshot'
     ?say('The export conflicts with this history. Register a separate branch.','Экспорт конфликтует с этой историей. Зарегистрируйте отдельную ветку.')
     :say('The export could not be bound. Refresh the session list.','Не удалось привязать экспорт. Обновите список сессий.');
   $('branchStatus').textContent=reason;return;
  }
  await refreshCampaigns();
  $('campaign').value=result.campaign;
  await loadSnapshots();
  $('branchStatus').textContent=say('Campaign selected: ','Кампания выбрана: ')+result.campaign.slice(0,8);
  await refreshPlayer();
 }catch{
  $('branchStatus').textContent=say('The server did not respond.','Сервер не ответил.');
 }finally{binding=false;await refreshPendingSessions();}
}
$('newBranch').onclick=()=>bindBranch(null);
$('resumeBranch').onclick=()=>{if($('campaign').value)bindBranch($('campaign').value)};
if(new URLSearchParams(location.search).get('panel')==='recognition'){
 $('atlasTab').click();
 $('mode').value='recognition';
}
window.addEventListener('eyes-workspace',event=>{
 if(['atlas','recognition','maker'].includes(event.detail))document.body.dataset.workspaceContext=event.detail;
 $((event.detail==='maker'?'maker':'atlas')+'Tab').click();
 if(event.detail==='recognition'&&$('mode').value!=='recognition'){
  $('mode').value='recognition';paint();
 }
 if(event.detail==='atlas'&&$('mode').value==='recognition'){
  $('mode').value='political';paint();
 }
});
translate();init();refreshCampaigns();refreshPlayer();refreshPendingSessions();
let polling=false;
setInterval(async()=>{
 if(polling||document.hidden||window.frameElement?.hidden)return;
 polling=true;
 try{await Promise.all([refreshPlayer(),refreshPendingSessions(),refreshCampaigns(true)]);}
 finally{polling=false;}
},5000);
