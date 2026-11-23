"""Conference lifecycle attached to existing chat/memory, never a treaty authority."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,uuid
from historical_people import load_people,eligibility


def normalize(value):
    if value is None:return {'schema_version':1,'conferences':{},'personal_relations':{},'native_clocks':{}}
    if not isinstance(value,dict) or value.get('schema_version')!=1:raise ValueError('Invalid historical diplomacy state')
    if not all(isinstance(value.get(k),dict) for k in ('conferences','personal_relations')):raise ValueError('Invalid conference indexes')
    result=deepcopy(value)
    result.setdefault('native_clocks',{})
    if not isinstance(result['native_clocks'],dict):raise ValueError('Invalid native clock index')
    return result


def observed_clock(chat):
    rows=[r for values in chat.get('game_context_observations',{}).get('by_country',{}).values() for r in values
          if r.get('campaign_id')==chat['campaign_id'] and r.get('source','').startswith('bridge_parsed_hoi4_game_log_')]
    if not rows:raise ValueError('A genuine game-day observation is required; reopen the country in HOI4')
    row=max(rows,key=lambda r:r['observed_at']);context=row['context']
    raw=context.get('game_day')
    if raw is None or context.get('game_day_basis')!='global.num_days':raise ValueError('Observed global.num_days is required')
    day=int(raw)
    if day<0:raise ValueError('Invalid game day')
    calendar=None
    for fmt in ('%Y-%m-%d','%B %d, %Y'):
        try:calendar=datetime.strptime(str(context.get('date')),fmt).date().isoformat();break
        except ValueError:pass
    return {'day':day,'date':calendar,'request_id':row['request_id'],'context':context,'country':row['country'],
            'observed_at':row['observed_at']}


_CONFERENCE_DRAFT_STATES = {'draft', 'negotiating', 'awaiting_approval'}


def _draft_revisions(chat):
    revisions = {}
    for agreement_id, agreement in chat.get('agreements', {}).items():
        version = agreement.get('current_version')
        versions = agreement.get('versions') or []
        terms = versions[version - 1].get('terms', []) if type(version) is int and 0 < version <= len(versions) else []
        # Bind to the diplomatic text, version and consent. Packaging and
        # execution bookkeeping do not make an old treaty a conference result.
        payload = [version, [
            {key: term.get(key) for key in ('term_id', 'status', 'clause', 'proposal_spec',
                                           'confirmed_by', 'confirmed_by_request_ids')}
            for term in terms
        ]]
        revisions[agreement_id] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return revisions


class Conferences:
    def __init__(self,engine):
        self.engine=engine
        self.state=engine.diplomatic_memory.setdefault('historical_diplomacy',normalize(None))

    def clock(self,chat):
        try:pair=observed_clock(chat)
        except ValueError:pair=None
        native=self.state.get('native_clocks',{}).get(chat['campaign_id'])
        if native and set(chat['participants'])<=set(native['countries']):
            if not pair or native['observed_at']>pair['observed_at']:
                # A day counter does not establish a historical calendar date.
                return {**native,'date':pair['date'] if pair and pair['day']==native['day'] else None,'context':{}}
        if pair:return pair
        raise ValueError('A genuine game-day observation is required; reopen the country in HOI4')

    def native_snapshot(self,campaign,snapshot):
        """Called only after the authenticated native route validated a new snapshot."""
        self.state.setdefault('native_clocks',{})[campaign]={
            'day':snapshot['day'],'countries':list(snapshot['countries']),
            'request_id':f"native:{snapshot['sequence']}",'source':'authenticated_native_snapshot',
            'observed_at':datetime.now(timezone.utc).isoformat()}
        active=sorted((c for c in self.state['conferences'].values()
                       if c['campaign_id']==campaign and c['status']=='active'
                       and set(c['countries']) <= snapshot['countries'].keys()),key=lambda c:(c['last_day'],c['conference_id']))
        advanced=[]
        for conf in active[:8]:
            if snapshot['day']<conf['last_day']:
                conf['status']='suspended';conf['outcome']='earlier_save_requires_matching_conference_snapshot'
            else:self.advance(conf['chat_id'],conf['conference_id'])
            advanced.append(conf['conference_id'])
        return advanced

    def for_chat(self,chat_id):
        chat=self.engine._chat(chat_id)
        return [c for c in self.state['conferences'].values() if c['chat_id']==chat_id and c['campaign_id']==chat['campaign_id']]

    def active(self,chat_id):
        return next((c for c in reversed(self.for_chat(chat_id)) if c['status']=='active'),None)

    def create(self,chat_id,data):
        chat=self.engine._chat(chat_id)
        request=data.get('request_id')
        if not isinstance(request,str) or not request or len(request)>160:raise ValueError('request_id required')
        goal=data.get('goal')
        if not self.engine.action_registry.get(str(goal or '')):
            raise ValueError('Goal must be an existing typed diplomatic action')
        duration=data.get('duration_days',min(7,1+len(chat['participants'])))
        if type(duration) is not int or not 1<=duration<=7:raise ValueError('Conference duration must be 1–7 game days')
        secrecy=data.get('secrecy','open')
        if secrecy not in {'open','closed','secret'}:raise ValueError('Invalid secrecy')
        informal=data.get('informal',False)
        if not isinstance(informal,bool):raise ValueError('informal must be Boolean')
        fingerprint=hashlib.sha256(json.dumps([goal,duration,secrecy,informal],sort_keys=True).encode()).hexdigest()
        previous=next((c for c in self.for_chat(chat_id) if c['request_id']==request),None)
        if previous:
            if previous['request_fingerprint']!=fingerprint:raise ValueError('Conference request replay changed parameters')
            return previous
        if chat['state']=='closed':raise ValueError('This chat is closed')
        if self.active(chat_id):raise ValueError('This chat already has an active conference')
        clock=self.clock(chat)
        if data.get('autonomous'):
            # No unsupported historical meetings. Even OFF needs explicit enable.
            from v6_autonomy import status
            snapshot=status(self.engine.diplomatic_memory,chat['campaign_id']).get('snapshot') or {}
            if snapshot.get('historical_focuses') is not False or not self.engine.autonomous_initiatives_enabled:
                raise ValueError('Autonomous conferences are disabled for historical/unknown policy')
            state=status(self.engine.diplomatic_memory,chat['campaign_id'])
            origin=chat.get('autonomous_origin') or {}
            proposal=state.get('inbox',{}).get(origin.get('proposal_id'))
            if (chat.get('chat_mode')!='ai_to_ai' or not proposal or proposal.get('chat_id')!=chat_id
                    or proposal.get('action_id')!=goal):
                raise ValueError('A justified existing AI initiative is required')
            from autonomous_negotiation import _evidence
            fresh,_=_evidence(self.engine,chat['campaign_id'],proposal)
            if fresh['day']!=clock['day']:raise ValueError('Conference snapshot is not current')
            recent=[c for c in self.state['conferences'].values() if c['campaign_id']==chat['campaign_id']
                    and set(c['countries'])&set(chat['participants']) and clock['day']-c['start_day']<90]
            if recent:raise ValueError('Country conference cooldown: 90 game days')
        conf={'conference_id':uuid.uuid4().hex,'campaign_id':chat['campaign_id'],'chat_id':chat_id,
              'request_id':request,'request_fingerprint':fingerprint,'countries':list(chat['participants']),
              'goal':goal,'secrecy':secrecy,'informal':informal,'start_day':clock['day'],
              'end_day':clock['day']+duration,'last_day':clock['day']-1,'calendar_date':clock['date'],
              'status':'active','stage':'opening','delegations':{t:[] for t in chat['participants']},
              'private_discussions':{},'positions':{},'events':[],'reports':[],
              'opening_draft_revisions':_draft_revisions(chat),
              'leader_ids':{},'simulation_only':True,'game_effect_applied':False,'outcome':None}
        for tag in chat['participants']:
            conf['delegations'][tag].append({'person_id':None,'display_name':tag,
                'role':'state_representative_unknown','mandate':'unverified','authority_to_sign':False})
        self.state['conferences'][conf['conference_id']]=conf
        if clock['date']:
            for tag in conf['countries']:
                candidates=[p for p in load_people().values() if p['country']==tag and eligibility(p,clock['date'],official=True)=='eligible']
                def relevance(p):
                    roles=' '.join(r['role'] for r in p['offices'] if r['start']<=clock['date']<r['end']).lower()
                    return (0 if any(t in roles for t in ('foreign','secretary_of_state','ambassador')) else 1,p['id'])
                for person in sorted(candidates,key=relevance)[:3]:self.invite(chat_id,conf['conference_id'],person['id'])
        from conference_phrases import render
        conf['events'].append({'kind':'opening','display_text':render('opening',conf['conference_id']),
                               'historical_quote':False,'original_text':None,'translation':None})
        return conf

    def review_plan(self,chat_id,country,request_id,positions):
        self.record_positions(chat_id,{'speaker':country,'request_id':request_id,
                                      'dialogue_positions':positions,'phase':'before_expression'})

    def review_player_text(self,chat_id,entry):
        conf=self.active(chat_id)
        if not conf:return
        country=self.engine._chat(chat_id)['player_country'];request=entry['request_id']
        reviews=conf['private_discussions'].setdefault(country,[])
        if any(r['source_request_id']==request for r in reviews):return
        reviews.append({'source_request_id':request,'phase':'player_text_review','text_unchanged':True,
            'opinions':[{'person_id':d.get('person_id'),'display_name':d['display_name'],
                         'personal_stance':'review_proposed_text','official_stance':'player_text_unchanged',
                         'basis':'advisory_only_no_ratification','binding':False,'historical_quote':False}
                        for d in conf['delegations'][country] if d.get('availability')!='historical_date_unavailable']})

    def get(self,chat_id,conference_id):
        conf=next((c for c in self.for_chat(chat_id) if c['conference_id']==conference_id),None)
        if conf is None:raise KeyError('Conference not found in this chat/campaign')
        return conf

    def invite(self,chat_id,cid,person_id):
        conf=self.get(chat_id,cid);chat=self.engine._chat(chat_id);clock=self.clock(chat)
        person=load_people().get(person_id)
        if person is None:raise ValueError('No sourced biography for this person')
        if person['country'] not in conf['countries']:raise ValueError('Person belongs to a different delegation')
        if conf['status']!='active':raise ValueError('Conference is no longer active')
        if not clock['date']:return {'person_id':person_id,'response':'pending','reason':'calendar_date_unknown'}
        reason=eligibility(person,clock['date'])
        if reason=='life_dates_unverified':return {'person_id':person_id,'response':'pending','reason':reason}
        if reason!='eligible':return {'person_id':person_id,'response':'decline','reason':reason,'in_world_cause':None}
        busy=any(c['conference_id']!=cid and c['status']=='active' and c['campaign_id']==conf['campaign_id']
                 and any(d.get('person_id')==person_id for ds in c['delegations'].values() for d in ds)
                 for c in self.state['conferences'].values())
        if busy:return {'person_id':person_id,'response':'reschedule','reason':'already_attending_conference','suggested_day':clock['day']+7}
        delegation=conf['delegations'][person['country']]
        if not any(d.get('person_id')==person_id for d in delegation):
            delegation.append({'person_id':person_id,'display_name':person['name'],'role':'invited_adviser',
                'mandate':'advisory_only','authority_to_sign':False,'historical_quote':False})
        return {'person_id':person_id,'response':'accept_advisory_invitation','authority_to_sign':False}

    def record_positions(self,chat_id,entry):
        conf=self.active(chat_id)
        if not conf:return
        speaker=entry.get('speaker');request=entry.get('request_id')
        if speaker not in conf['countries'] or not request or not entry.get('dialogue_positions'):return
        if any(ev.get('source_request_id')==request for ev in conf['events']):return
        positions=deepcopy(entry['dialogue_positions']);conf['positions'][speaker]=positions
        from delegation_deliberation import deliberate
        review=deliberate(conf['delegations'][speaker],load_people(),positions,conf['goal'],conf['calendar_date'])
        # Unknown representatives retain the previous transparent advisory entry.
        if not review['opinions']:
            review['opinions']=[{'person_id':None,'display_name':speaker,'point':p.get('point'),
                'personal_stance':p['stance'],'official_stance':p['stance'],'basis':'unverified_state_representative',
                'historical_quote':False,'binding':False} for p in positions]
        conf['private_discussions'].setdefault(speaker,[]).append({'source_request_id':request,**review})
        conf['events'].append({'kind':'engine_position_reviewed','country':speaker,'source_request_id':request,
                               'phase':entry.get('phase','recorded_position')})

    def check_leadership(self,chat_id,observed_ids):
        """Internal hook only: IDs must come from a genuine matching game observation."""
        conf=self.active(chat_id)
        if not conf:return
        chat=self.engine._chat(chat_id)
        rows=[r for rs in chat.get('game_context_observations',{}).get('by_country',{}).values() for r in rs]
        for tag,new_id in observed_ids.items():
            if not isinstance(new_id,str) or not new_id:raise ValueError('Leader ID required')
            evidence=next((r for r in reversed(rows) if r.get('campaign_id')==chat['campaign_id'] and
                r.get('source','').startswith('bridge_parsed_hoi4_game_log_') and
                ((r.get('country')==tag and r['context'].get('leader_id')==new_id) or
                 (tag==chat['player_country'] and r['context'].get('player_leader_id')==new_id))),None)
            if evidence is None:raise ValueError('No game evidence for leader change')
            old=conf['leader_ids'].get(tag);conf['leader_ids'][tag]=new_id
            if old and old!=new_id:
                conf['status']='interrupted';conf['outcome']='leadership_changed'
                if tag==chat['player_country']:
                    chat['state']='closed';chat['closure_reason']='observed_leadership_change'
                conf['events'].append({'kind':'leadership_changed','country':tag,'old':old,'new':new_id,
                                      'source_request_id':evidence['request_id']})

    def personal_outcome(self,chat_id,cid,event_id,kind,left,right):
        conf=self.get(chat_id,cid)
        if not isinstance(event_id,str) or not event_id or len(event_id)>160:raise ValueError('Personal event identity required')
        people={d['person_id'] for ds in conf['delegations'].values() for d in ds if d.get('person_id')}
        if left not in people or right not in people or left==right:raise ValueError('Known attending people required')
        if kind not in {'constructive_meeting','ordinary_disagreement'}:raise ValueError('Promise breaches need verified game evidence; not inferred here')
        if conf['status'] not in {'completed','interrupted'}:raise ValueError('Meeting has not concluded')
        key=conf['campaign_id']+':'+':'.join(sorted([left,right]))
        row=self.state['personal_relations'].setdefault(key,{'trust':0.5,'events':{}})
        if event_id in row['events']:
            if row['events'][event_id]['kind']!=kind or row['events'][event_id]['conference_id']!=cid:raise ValueError('Personal event replay conflict')
            return deepcopy(row)
        if len(row['events'])>=100:raise ValueError('Personal event review required')
        delta=0.01 if kind=='constructive_meeting' else 0.0
        row['trust']=round(min(1,max(0,row['trust']+delta)),4)
        row['events'][event_id]={'kind':kind,'conference_id':cid,'delta':delta,'basis':'simulated_meeting_not_historical_fact'}
        return deepcopy(row)

    def advance(self,chat_id,cid,respond=False):
        conf=self.get(chat_id,cid);chat=self.engine._chat(chat_id)
        if conf['status']!='active':return conf
        if chat['state']=='closed':return self.close(chat_id,cid,reason='chat_closed')
        clock=self.clock(chat)
        if set(chat['participants'])!=set(conf['countries']):
            conf['status']='interrupted';conf['outcome']='participants_changed';return conf
        leader_ids={}
        for country,rows in chat.get('game_context_observations',{}).get('by_country',{}).items():
            if not rows:continue
            context=rows[-1]['context']
            if context.get('leader_id'):leader_ids[country]=context['leader_id']
            if context.get('player_leader_id'):leader_ids[chat['player_country']]=context['player_leader_id']
        if leader_ids:self.check_leadership(chat_id,leader_ids)
        if clock['day']<conf['last_day']:raise ValueError('Earlier save/day: restore matching EYES snapshot; no replay')
        if conf['status']!='active' or (clock['day']==conf['last_day'] and not respond):return conf
        # Only observed days advance the agenda. No real-time Ollama delay is a day.
        conf['last_day']=clock['day'];conf['calendar_date']=clock['date']
        if clock['date']:
            for delegates in conf['delegations'].values():
                for delegate in delegates:
                    p=load_people().get(delegate.get('person_id'))
                    if p:delegate['availability']='available' if eligibility(p,clock['date'])=='eligible' else 'historical_date_unavailable'
        else:
            for delegates in conf['delegations'].values():
                for delegate in delegates:
                    if delegate.get('person_id'):delegate['availability']='calendar_date_unverified'
        if clock['day']>=conf['end_day']:
            conf['status']='completed';conf['stage']='conclusion'
            conf['conference_drafts']=self.drafts(conf,chat)
            ready=any(a['status']=='awaiting_approval' for a in conf['conference_drafts'])
            conf['outcome']='preliminary_draft' if conf['informal'] else 'awaiting_player_confirmation' if ready else 'no_agreement'
            conf['events'].append({'kind':'conference_concluded','day':clock['day'],'outcome':conf['outcome'],
                                  'relations_penalty':0,'game_effect_applied':False})
        else:
            conf['stage']='negotiating'
            # Existing engine handles queued official replies. No invented player turn.
            if respond and not conf['informal'] and chat.get('response_queue') and conf.get('response_day')!=clock['day']:
                self.engine.advance_chat(chat_id,self.engine.get_chat(chat_id)['next_request_id'])
                conf['response_day']=clock['day']
        return conf

    def drafts(self,conf,chat):
        baseline=conf.get('opening_draft_revisions')
        if baseline is None:
            # Legacy conferences lack the evidence needed to attribute drafts.
            return []
        return [{'agreement_id':key,'version':agreement['current_version'],'status':agreement['status']}
                for key,revision in _draft_revisions(chat).items()
                if baseline.get(key)!=revision
                and (agreement:=chat['agreements'][key])['status'] in _CONFERENCE_DRAFT_STATES]

    def close(self,chat_id,cid,reason='closed_by_player'):
        conf=self.get(chat_id,cid)
        if conf['status']=='active':
            conf.update(status='interrupted',stage='conclusion',outcome=reason)
            conf['events'].append({'kind':'conference_concluded','day':max(conf['start_day'],conf['last_day']),
                                   'outcome':reason,'relations_penalty':0,'game_effect_applied':False})
        return conf

    def handoff(self,chat_id,cid):
        conf=self.get(chat_id,cid)
        if conf['informal']:raise ValueError('Informal draft requires a separate official negotiation')
        if 'agreement_handoff' not in conf:
            if conf['status'] not in {'active','completed'}:raise ValueError('Conference cannot hand off')
            chat=self.engine._chat(chat_id)
            if 'conference_drafts' not in conf:
                conf['conference_drafts']=self.drafts(conf,chat) if conf['status']=='active' else []
            conf['agreement_handoff']=deepcopy(conf['conference_drafts'])
            if conf['status']=='active':
                conf.update(status='completed',stage='conclusion',outcome='returned_to_official_confirmation')
                conf['events'].append({'kind':'conference_concluded',
                                       'day':max(conf['start_day'],conf['last_day']),
                                       'outcome':conf['outcome'],'relations_penalty':0,'game_effect_applied':False})
        return {'agreements':deepcopy(conf['agreement_handoff']),'confirmation_required':True,'game_effect_applied':False}

    def ensure_official(self,chat_id):
        conf=self.active(chat_id)
        if conf and conf['informal']:raise ValueError('Informal consultations cannot ratify a treaty; return to the official channel')
        if conf and not all(any(d.get('authority_to_sign') is True and d.get('mandate')=='verified_game_mandate'
                                for d in ds) for ds in conf['delegations'].values()):
            raise ValueError('Conference has no verified signing mandates. Finish it and use the existing official treaty confirmation channel.')

    def view(self,conf,viewer):
        member=viewer in conf['countries']
        if not member and conf['secrecy']=='secret':return None
        base={k:deepcopy(conf[k]) for k in ('conference_id','status','secrecy')}
        if not member and conf['secrecy']=='closed':return base
        base.update({k:deepcopy(conf[k]) for k in ('countries','goal','start_day','end_day','stage','outcome','informal')})
        if member:
            base.update(delegations=deepcopy(conf['delegations']),positions=deepcopy(conf['positions']),
                        own_internal_discussions=deepcopy(conf['private_discussions'].get(viewer,[])),
                        events=deepcopy(conf['events']),intelligence=[deepcopy(r) for r in conf['reports'] if r['viewer']==viewer])
            from conference_phrases import render
            base['phase_text'] = render('conclusion' if conf['status']!='active' else 'review', conf['conference_id'])
            base['representative_limitation'] = 'Game leader IDs and signing mandates are not exported by the current Bridge.'
        return base

    def respond_report(self,chat_id,cid,report_id,country,response):
        conf=self.get(chat_id,cid)
        if country not in conf['countries']:raise ValueError('Only a delegation may respond')
        if response not in {'confirm_meeting','deny_claim','decline_comment'}:raise ValueError('Unsupported public response')
        report=next((r for r in conf['reports'] if r['report_id']==report_id),None)
        if report is None:raise ValueError('No report to respond to')
        replies=report.setdefault('responses',{})
        if country in replies and replies[country]['response']!=response:raise ValueError('Conflicting report response replay')
        replies.setdefault(country,{'response':response,'country':country,'basis':report['basis'],
                                   'truth_guaranteed':False,'relations_delta':0})
        # A denial is a statement, not evidence that the report is false.
        return deepcopy(replies[country])

    def intelligence(self,chat_id,cid,viewer,simulation=False):
        conf=self.get(chat_id,cid)
        if not simulation:return {'available':False,'reason':'No verified HOI4 intelligence channel','reports':[]}
        # Opt-in laboratory model; never presented as obtained HOI4 intelligence.
        if viewer in conf['countries']:raise ValueError('Not foreign intelligence')
        clock=self.clock(self.engine._chat(chat_id));bucket=clock['day']//30
        key=f'{cid}:{viewer}:{bucket}'
        old=next((r for r in conf['reports'] if r['report_id']==key),None)
        if old:return old
        rare=int(hashlib.sha256(key.encode()).hexdigest()[:8],16)%100==0
        if not rare:return {'available':False,'basis':'simulation','reason':'No report in this game-month'}
        result={'report_id':key,'viewer':viewer,'day':clock['day'],'source':'simulated_diplomatic_rumour',
                'confidence':'low','truth_guaranteed':False,'claim':'A meeting may be taking place.',
                'full_text_disclosed':False,'basis':'simulation'}
        conf['reports'].append(result);return result
