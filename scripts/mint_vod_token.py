#!/usr/bin/env python3
"""铸造新 VOD token：重放 LTI 1.3 六跳链，从保存的 normandy cookie 出发，更新 bearer.txt/jwt.txt。
用法: python3 mint_vod_token.py   （成功则打印新 token 的 exp）"""
import urllib.request, urllib.parse, ssl, re, time, base64, json, os, sys
HOME = os.path.expanduser("~")
NORM = open(HOME+"/vod-research/credentials/normandy.txt").read().strip()
UA = {"User-Agent":"Mozilla/5.0","Cookie":"_normandy_session="+NORM}
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a): return None
op = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedir, urllib.request.HTTPSHandler(context=ctx))
def do(method,url,body=None,extra=None):
    h=dict(UA)
    if body is not None: h["Content-Type"]="application/x-www-form-urlencoded"
    if extra: h.update(extra)
    r=urllib.request.Request(url,data=body,headers=h)
    try:
        resp=op.open(r,timeout=40); return resp.status, resp.headers, resp.read(12000).decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read(12000).decode("utf-8","replace")
def inputs(page):
    f={}
    for it in re.findall(r'<input[^>]*>',page,re.I):
        n=re.search(r'name="([^"]+)"',it); v=re.search(r'value="([^"]*)"',it)
        if n: f[n.group(1)]=v.group(1) if v else ""
    return f
COURSE=94157; TOOL=8329
s,_,fresh = do("GET","https://oc.sjtu.edu.cn/courses/%d/external_tools/%d?display=borderless"%(COURSE,TOOL))
if s!=200: print("launch page HTTP %d (normandy 会话可能已失效)"%s); sys.exit(2)
f1=inputs(fresh)
s,h,_=do("POST",f1["target_link_uri"],urllib.parse.urlencode(f1).encode(),{"Referer":"https://oc.sjtu.edu.cn/courses/%d"%COURSE})
s,h,_=do("GET",h.get("Location",""))
s,h,page=do("GET",h.get("Location",""))
f2=inputs(page)
fm=re.search(r'<form[^>]*action="([^"]+)"',page,re.I); launch=fm.group(1)
s,h,b=do("POST",launch,urllib.parse.urlencode(f2).encode(),{"Referer":"https://oc.sjtu.edu.cn/api/lti/authorize","Origin":"https://oc.sjtu.edu.cn"})
loc=h.get("Location","")
m=re.search(r"jwt_token=([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",loc)
if not m: print("未拿到 jwt_token (launch -> HTTP %d)"%s); sys.exit(3)
tok=m.group(1)
def b64u(x):
    x+=("="*(-len(x)%4)); return base64.urlsafe_b64decode(x).decode("utf-8","replace")
pay=json.loads(b64u(tok.split(".")[1])); acc=pay["accessToken"]; exp=pay["exp"]
open(HOME+"/vod-research/blockchain-apps/scripts/jwt.txt","w").write(tok)
open(HOME+"/vod-research/blockchain-apps/scripts/bearer.txt","w").write(acc)
import datetime
print("铸成功: sub=%s exp=%s UTC 剩余%.1fh (已写 bearer.txt/jwt.txt)"%(pay.get("sub"),datetime.datetime.utcfromtimestamp(exp).strftime("%Y-%m-%d %H:%M"),(exp-time.time())/3600))
