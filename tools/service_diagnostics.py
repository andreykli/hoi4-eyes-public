"""Read-only desktop probes. Never load/download a model or start a process."""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from model_profile import selected_model

def get_json(path):
    with urllib.request.urlopen('http://127.0.0.1:11434/api/'+path,timeout=2) as response:
        data=json.load(response)
    if (not isinstance(data,dict) or not isinstance(data.get('models'),list)
            or any(not isinstance(row,dict) for row in data['models'])):
        raise ValueError('invalid_model_inventory')
    return data['models']

def diagnose(backend,fetch=get_json,warmup=None):
    started=time.monotonic()
    try:base=backend.status()
    except Exception:base={'error':'probe_failed'}
    services={}
    for name,key,owner in [('Flask','flask','server'),('Bridge','bridge','bridge')]:
        row=base.get(key,{})
        if row.get('ready') is True:
            state,reason=('working','verified') if row.get('current_revision') is True else ('error','revision_mismatch')
        elif base.get('error'):state,reason='error','probe_failed'
        elif owner in base.get('owned',[]):
            stamp=backend.owned.get(owner,{}).get('started',0)
            state,reason=('starting','waiting_readiness') if time.monotonic()-stamp<60 else ('error','not_ready')
        else:state,reason='unavailable','not_ready'
        services[name]={'state':state,'reason':reason}
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests={key:pool.submit(fetch,key) for key in ('tags','ps')}
        values={};errors=set();invalid=False
        for key,future in requests.items():
            try:values[key]=future.result()
            except ValueError:errors.add(key);invalid=True
            except Exception:errors.add(key)
    model=selected_model()
    installed=any(row.get('name',row.get('model'))==model for row in values.get('tags',[])) if 'tags' not in errors else None
    loaded=any(row.get('name',row.get('model'))==model for row in values.get('ps',[])) if 'ps' not in errors else None
    services['Ollama']={'state':'working' if not errors else 'unavailable' if len(errors)==2 else 'error',
        'reason':'api_ready' if not errors else 'api_unavailable' if len(errors)==2 else 'inventory_failed'}
    if errors:state,reason='unavailable' if len(errors)==2 else 'error','api_unavailable' if len(errors)==2 else 'inventory_failed'
    elif loaded:state,reason='working','loaded'
    elif not installed:state,reason='unavailable','model_missing'
    elif warmup and warmup.get('state')=='starting' and time.monotonic()-warmup.get('started',0)<65:
        state,reason='starting','model_loading'
    elif warmup and warmup.get('state')=='error':state,reason='error','model_load_failed'
    else:state,reason='unavailable','installed_not_loaded'
    services['Qwen']={'state':state,'reason':reason,'model':model,'installed':installed,'loaded':loaded}
    if invalid:
        for name in ('Ollama','Qwen'):services[name].update(state='error',reason='inventory_failed')
    return {'services':services,'checked_at':time.time(),'probe_seconds':round(time.monotonic()-started,3),
            **{name:row['state']=='working' for name,row in services.items()}}
