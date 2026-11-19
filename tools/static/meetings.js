'use strict';
const $ = id => document.getElementById(id);
const labels = JSON.parse($('meeting-labels').textContent);
let scope = null;
let records = new Map();
let busy = false;
let requestId = null;
let lastAction = null;
let autoDiscussion = false;
let intervention = null;
let roster = [];

function element(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}

async function api(path, body) {
  const result = await fetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)} : {cache:'no-store'});
  const data = await result.json();
  if (!result.ok) {
    const code = {
      meeting_not_due:'not_due', meeting_changed:'stale', meeting_context_required:'stale',
      meeting_participant_unavailable:'unverified_role', meeting_discussion_limit:'limit',
      meeting_discussion_unavailable:'reply_error',
    }[data.error];
    throw new Error(labels[code] || labels.error);
  }
  return data;
}

const roleLabel = person => person.role === 'minister' && person.name ? labels.adviser : labels.role_labels[person.role];
const personLabel = person => person.name ? `${person.name} · ${roleLabel(person)}` : roleLabel(person);

function renderSeats() {
  const privateMeeting = $('format').value === 'private';
  const checkboxes = [...$('schedule').querySelectorAll('input[type=checkbox]')];
  if (privateMeeting) {
    const current = checkboxes.find(input => input.checked);
    checkboxes.forEach(input => {input.checked = input === current;});
  }
  for (const input of checkboxes) {
    const select = document.querySelector(`select[data-seat="${input.value}"]`);
    const value = select.value;
    const available = roster.filter(person => person.role === input.value);
    select.replaceChildren(new Option(labels.generic, ''), ...available.map(person =>
      new Option(`${personLabel(person)} · ${person.country}`, person.id)));
    if (available.some(person => person.id === value)) select.value = value;
    else if (input.value === 'head' && available.length === 1 && select.dataset.seen !== 'yes') select.value = available[0].id;
    if (available.length) select.dataset.seen = 'yes';
    select.hidden = !input.checked || !available.length;
  }
}

function render() {
  const selected = records.get($('meeting').value);
  const events = selected?.events ?? [];
  const summary = events[0];
  $('meeting-heading').textContent = summary?.title ?? labels.choose;
  const closed = events.at(-1)?.kind === 'closed';
  const due = summary && summary.scheduled_day <= scope?.day;
  const editable = scope && !$('archive').value && summary && !selected.has_future && !closed && due;
  const discussion = editable && summary.format === 'discussion';
  $('turn').hidden = !editable;
  $('turn').querySelector('[type=submit]').textContent = discussion ? labels.intervene : labels.send;
  $('discussion-controls').hidden = !discussion;
  $('continue-discussion').disabled = autoDiscussion || busy;
  $('stop-discussion').disabled = !autoDiscussion;
  $('positions').hidden = !summary;
  $('project').hidden = !editable;
  $('motion').hidden = !editable;
  $('close-meeting').hidden = !editable;
  const history = $('history');
  const projects = $('projects');
  history.replaceChildren();
  projects.replaceChildren();
  $('position-list').replaceChildren();
  if (!summary) return;
  history.append(element('p', summary.participants.map(personLabel).join(' / ')));
  history.append(element('p', `${labels.day} ${summary.scheduled_day}`));
  if ($('archive').value) history.append(element('p', labels.archive));
  else if (selected.has_future) history.append(element('p', labels.future));
  else if (closed) history.append(element('p', labels.closed));
  else if (!due) history.append(element('p', labels.not_due));
  const latest = new Map();
  for (const item of events.slice(1)) {
    if (item.kind === 'turn' || item.kind === 'exchange') {
      const row = element('article', '');
      if (item.text) row.append(element('strong', labels.player), element('p', item.text));
      if (item.task === 'compromise') row.append(element('p', labels.compromise));
      for (const reply of item.replies) {
        row.append(element('strong', `${personLabel(reply)} · ${labels.kinds[reply.kind]}`),
                   element('p', reply.text));
        latest.set(reply.role, reply);
      }
      if (!item.replies.length) row.append(element('p', item.kind === 'turn' ? labels.unavailable : labels.reply_error));
      history.append(row);
    } else if (item.kind === 'paused') {
      history.append(element('p', labels.paused));
    } else if (item.kind === 'decision') {
      history.append(element('p', `${labels.decision} · ${labels.day} ${item.day} · ${item.text}`));
    } else if (item.kind === 'motion') {
      const motion = element('article', '');
      motion.append(element('p', item.text));
      const decision = events.find(row => row.motion === item.id);
      item.options.forEach((option, index) => {
        if (editable && !decision) {
          const button = element('button', option);
          button.onclick = () => {
            if (confirm(labels.confirm)) change({action:'choose', motion:item.id, choice:index, confirmed:true});
          };
          motion.append(button);
        } else {
          motion.append(element('p', decision?.text === option ? `${labels.selected} · ${option}` : option));
        }
      });
      history.append(motion);
    } else if (item.kind === 'project') {
      const project = element('article', '');
      project.append(element('h3', item.project.title));
      const list = element('ol', '');
      const reports = events.filter(e => e.kind === 'milestone' && e.project === item.project.id);
      item.project.stages.forEach((stage, i) => {
        list.append(element('li', `${stage.text} · ${labels.day} ${stage.due}` +
          (reports[i] ? ` · ${labels.progress}: ${reports[i].text}` : '')));
      });
      project.append(list);
      if (editable && reports.length < item.project.stages.length) {
        const button = element('button', labels.finish);
        button.onclick = () => {
          const evidence = window.prompt(labels.finish);
          if (evidence?.trim() && confirm(labels.confirm)) {
            change({action:'milestone', project:item.project.id, text:evidence, confirmed:true});
          }
        };
        project.append(button);
      }
      projects.append(project);
    }
  }
  for (const reply of latest.values()) {
    $('position-list').append(element('li', `${personLabel(reply)} · ${labels.stances[reply.stance] || labels.stances.undecided} · ${reply.text}`));
  }
}

async function refresh(selectedId = $('meeting').value) {
  try {
    const data = await api('/meetings/state');
    const selectedArchive = $('archive').value;
    scope = data.scope;
    roster = data.roster;
    renderSeats();
    $('archive').replaceChildren(new Option(labels.live, ''), ...data.archives.map(row =>
      new Option(`${labels.archive} · ${row.player} · ${row.campaign.slice(0, 8)}`, `${row.campaign}:${row.player}`)));
    $('archive').value = [...$('archive').options].some(option => option.value === selectedArchive) ? selectedArchive : '';
    let meetings = data.meetings;
    if ($('archive').value) {
      const [campaign, player] = $('archive').value.split(':');
      ({meetings} = await api('/meetings/archive?' + new URLSearchParams({campaign,player})));
    }
    records = new Map(meetings.map(meeting => [meeting.id, meeting]));
    $('context').textContent = scope ? [scope.player, scope.date, scope.campaign.slice(0, 8)].join(' · ') : labels.scope;
    $('schedule').querySelector('button').disabled = !scope || !!$('archive').value;
    $('meeting').replaceChildren(new Option(labels.choose, ''), ...meetings.map(
      meeting => new Option(meeting.events[0].title, meeting.id)));
    $('meeting').value = records.has(selectedId) ? selectedId : '';
    $('headlines').replaceChildren();
    $('press-context').textContent = '';
    render();
  } catch (error) {
    scope = null;
    roster = [];
    renderSeats();
    records.clear();
    $('context').textContent = error.message;
    $('schedule').querySelector('button').disabled = true;
    render();
  }
}

async function change(args, target = $('meeting').value) {
  if (busy || !scope || $('archive').value) return false;
  busy = true;
  const usesModel = ['turn','continue','compromise'].includes(args.action);
  $('message').textContent = usesModel ? labels.generating : '';
  if (usesModel) $('message').setAttribute('aria-busy','true');
  const payload = {scope, meeting:target || null, revision:records.get(target)?.events.length, ...args};
  const signature = JSON.stringify(payload);
  if (lastAction !== signature) requestId = crypto.randomUUID().replaceAll('-', '');
  lastAction = signature;
  try {
    const result = await api('/meetings/change', {...payload, command: requestId});
    if (args.action === 'schedule') {
      $('subject').value = '';
      $('meeting').append(new Option(result.meeting.events[0].title, result.meeting.id));
    }
    records.set(result.meeting.id, result.meeting);
    $('meeting').value = result.meeting.id;
    render();
    $('message').textContent = '';
    lastAction = null;
    return true;
  } catch (error) {
    if (args.action !== 'continue' || autoDiscussion) $('message').textContent = error.message;
    return false;
  } finally {
    busy = false;
    $('continue-discussion').disabled = autoDiscussion;
    $('message').removeAttribute('aria-busy');
    if (intervention !== null) {
      const text = intervention;
      intervention = null;
      submitTurn(text);
    }
  }
}

async function continueDiscussion() {
  if (busy || autoDiscussion) return;
  const target = $('meeting').value;
  autoDiscussion = true;
  render();
  for (let i=0;i<labels.round_size && autoDiscussion && $('meeting').value===target;i++) {
    if (!await change({action:'continue'},target)) break;
    if (!records.get(target)?.events.at(-1)?.replies?.length) break;
  }
  autoDiscussion = false;
  render();
}

async function stopDiscussion() {
  autoDiscussion = false;
  const target = $('meeting').value;
  if (!scope || !records.has(target)) return;
  try {
    const data = await api('/meetings/change',{scope,meeting:target,revision:records.get(target).events.length,
      action:'stop',command:crypto.randomUUID().replaceAll('-','')});
    if (records.get(target).events.length <= data.meeting.events.length) records.set(target,data.meeting);
    render();
  } catch {
    if (!busy) refresh(target);
  }
}

async function submitTurn(text) {
  if (await change({action:'turn',text})) $('message-text').value = '';
}

$('meeting').onchange = render;
$('archive').onchange = () => { if (!busy) refresh(''); };
$('refresh').onclick = () => {if (!busy) refresh();};
$('format').onchange = renderSeats;
$('schedule').querySelector('fieldset').onchange = event => {
  if (event.target.type !== 'checkbox') return;
  if ($('format').value === 'private' && event.target.checked) {
    $('schedule').querySelectorAll('input[type=checkbox]').forEach(input => {input.checked = input === event.target;});
  }
  renderSeats();
};
$('continue-discussion').onclick = continueDiscussion;
$('stop-discussion').onclick = stopDiscussion;
$('compromise').onclick = () => change({action:'compromise'});
$('schedule').onsubmit = event => {
  event.preventDefault();
  const roles = [...$('schedule').querySelectorAll('input[type=checkbox]:checked')].map(node => node.value);
  const format = $('format').value;
  if (!roles.length || (format === 'private') !== (roles.length === 1)) {
    $('message').textContent = labels.format_error;
    return;
  }
  const people = Object.fromEntries(roles.map(role => [role, document.querySelector(`select[data-seat="${role}"]`).value || null]));
  change({action:'schedule', title:$('subject').value, mode:$('mode').value,
    topic:$('topic').value, delay:Number($('delay').value), roles, format, people}, '');
};
$('turn').onsubmit = async event => {
  event.preventDefault();
  const text = $('message-text').value;
  if (busy && autoDiscussion) {
    intervention = text;
    await stopDiscussion();
  } else {
    autoDiscussion = false;
    submitTurn(text);
  }
};
$('approve').onclick = async () => {
  if ($('message-text').reportValidity() && confirm(labels.confirm)
      && await change({action:'decision', text:$('message-text').value, confirmed:true})) $('message-text').value = '';
};
$('project').onsubmit = async event => {
  event.preventDefault();
  const stages = $('project-stages').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (confirm(labels.confirm) && await change({action:'project', title:$('project-title').value, stages,
      duration:Number($('duration').value), confirmed:true})) $('project').reset();
};
$('motion').onsubmit = async event => {
  event.preventDefault();
  const options = $('options').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (await change({action:'motion', text:$('motion-title').value, options})) $('motion').reset();
};
$('close-meeting').onclick = () => {
  if (confirm(labels.confirm)) change({action:'close', confirmed:true});
};
$('news').onclick = async () => {
  try {
    const data = await api('/meetings/press?view=' + $('press').value);
    $('press-context').textContent = labels.lenses[data.view];
    $('headlines').replaceChildren(...(data.news.length ? data.news.map(row => element('li', row.headline))
      : [element('li', labels.empty_news)]));
  } catch (error) {
    $('message').textContent = error.message;
  }
};
refresh();
