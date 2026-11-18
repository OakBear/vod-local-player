import os,json,base64,urllib.request,urllib.error,concurrent.futures,time,tempfile
ROOT=os.path.expanduser('~/vod-research')
token_file=os.path.join(ROOT,'blockchain-apps','scripts','jwt.txt')
raw=open(token_file).read().strip().split('.')[1]
token=json.loads(base64.urlsafe_b64decode(raw+'='*((-len(raw))%4)))['accessToken']
base='https://v.sjtu.edu.cn/jy-application-resourcemanage'
direct=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def api(path):
 req=urllib.request.Request(base+path,headers={'Authorization':'Bearer '+token,'User-Agent':'Mozilla/5.0'})
 with direct.open(req,timeout=25) as r: result=json.load(r)
 if result.get('status') != 200 or not result.get('ok',True): raise ValueError('API returned status '+str(result.get('status')))
 return result.get('data')
def work(item):
 directory,s=item; sid=s.get('id'); label=s.get('label'); dest=os.path.join(directory,'subtitles',label+'.nodes.json')
 if os.path.exists(dest):return 'existing'
 try:
  ppt=api('/v1/course/ai/ppt?courseId='+str(sid)) or {}
  summary=api('/v1/course/ai/textSummary/'+str(sid)) or {}
  slides=[]
  for p in (ppt.get('docList') or []):
   if not isinstance(p,dict):continue
   slides.append({k:p[k] for k in ('imageId','imageSeekTime','ocrText','textKeywordList','showFlag') if k in p})
  data={'courseId':sid,'slides':slides,'summary':summary.get('summary') if isinstance(summary,dict) else None,'mindmap':summary.get('mindmap') if isinstance(summary,dict) else None}
  os.makedirs(os.path.dirname(dest),exist_ok=True)
  with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=os.path.dirname(dest),delete=False) as f: json.dump(data,f,ensure_ascii=False); tmp=f.name
  os.replace(tmp,dest)
  return 'saved'
 except Exception as e:return 'failed '+str(sid)+' '+type(e).__name__
items=[]
for c in json.load(open(os.path.join(ROOT,'backend','catalog.json'))):
 directory=os.path.join(ROOT,c['dir']); manifest=os.path.join(directory,'manifest.json')
 if os.path.isfile(manifest):
  for s in json.load(open(manifest)).get('sessions',[]):
   if s.get('id') and s.get('label'):items.append((directory,s))
print('Sessions:',len(items),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
 counts={}
 for n,result in enumerate(pool.map(work,items),1):
  kind=result.split()[0];counts[kind]=counts.get(kind,0)+1
  if kind=='failed':print(result,flush=True)
  if n%15==0 or n==len(items):print('Progress',n,'/',len(items),counts,flush=True)
