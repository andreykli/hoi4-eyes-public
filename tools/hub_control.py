"""Local UI commands and capability-gated, idle-only shutdown of owned Flask."""
import hashlib,hmac,json,os,time,threading,uuid
from pathlib import Path

def atomic_json(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
 tmp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8');tmp.replace(path)

def signature(payload,secret):
 return hmac.new(secret.encode(),json.dumps(payload,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()

def command(path,pid,operation,secret,**values):
 body={'request_id':uuid.uuid4().hex,'pid':pid,'operation':operation,'issued':time.time(),**values}
 atomic_json(path,{'body':body,'signature':signature(body,secret)})
 return body['request_id']

class CommandReader:
 def __init__(self,path,pid,bridge_token,owner_token=None):
  self.path=Path(path);self.pid=pid;self.bridge_token=bridge_token;self.owner_token=owner_token;self.seen=[]
  self.started=time.time()
 def read(self,now=None):
  if not self.path.exists() or self.path.stat().st_size>16384:return None
  try:
   value=json.loads(self.path.read_text(encoding='utf8'));body=value['body'];op=body['operation']
   secret=self.owner_token if op=='shutdown' else self.bridge_token
   if (not secret or op not in {'open_chat','shutdown'} or body['pid']!=self.pid
       or body['request_id'] in self.seen or body['issued']<self.started
       or not 0<=(time.time() if now is None else now)-body['issued']<=30
       or not hmac.compare_digest(value['signature'],signature(body,secret))):return None
   self.seen=(self.seen+[body['request_id']])[-128:];return body
  except (OSError,ValueError,KeyError,TypeError):return None

def attach_server_control(app,owner_token,shutdown):
 if not owner_token:return
 from flask import request,jsonify,g
 lock=threading.Lock();active=[0];stopping=[False]
 @app.before_request
 def track():
  with lock:
   if stopping[0] and request.path!='/experimental-control/shutdown':
    return jsonify(error='shutting_down'),503
   active[0]+=1
  g.hub_request_active=True
 @app.teardown_request
 def untrack(_error):
  if getattr(g,'hub_request_active',False):
   with lock:active[0]-=1
   g.hub_request_active=False
 @app.post('/experimental-control/shutdown')
 def stop():
  supplied=request.headers.get('X-EYES-Owner','')
  if not supplied.isascii() or not hmac.compare_digest(supplied,owner_token):return jsonify(error='owner_required'),403
  with lock:
   if active[0]>1:return jsonify(error='busy',detail='A request is still active; nothing stopped.'),409
   stopping[0]=True
  threading.Thread(target=shutdown,daemon=True).start()
  return jsonify(status='shutdown_requested',pid=os.getpid())
