"""小规模真实 API 验证，只发送合成对话；不写系统提醒事项。"""
import json,tempfile,time
from datetime import datetime,timedelta
from pathlib import Path
from server import Engine
from core import TZ,validate_item,review_batch
now=datetime.now(TZ);anchor=now-timedelta(minutes=5);due=(anchor+timedelta(days=1)).replace(hour=15,minute=0,second=0,microsecond=0).isoformat()
def m(mid,text,who='1',offset=0):return {'id':mid,'text':text,'direction':who,'ts':int((anchor+timedelta(minutes=offset)).timestamp()*1000),'kind':'text','truncated':False}
cases=[('明确OK',[m('a','明天下午三点我们开项目会议，你能参加吗？'),m('b','OK','2',1)],'auto'),('未回复',[m('a','明天下午三点我们开项目会议，你能参加吗？')],'review'),('明确拒绝',[m('a','明天下午三点我们开项目会议，你能参加吗？'),m('b','不去，我没空','2',1)],'none'),('跨话题',[m('a','明天下午三点开项目会议，你来吗？'),m('b','我给你发一个电影推荐，看看吧',offset=1),m('c','好的','2',2)],'not_auto')]
results=[]
with tempfile.TemporaryDirectory() as tmp:
 e=Engine(tmp);e.store.set_setting('daily_budget',30000)
 for name,ms,expected in cases:
  started=time.time();r=e.extract(ms,[m['id'] for m in ms],[],{'kind':1})
  checked=review_batch(r['items'],ms,[m['id'] for m in ms])
  auto=any(x['auto'] for x in checked);n=len(checked)
  passed={'auto':auto,'review':n>0 and not auto,'none':n==0,'not_auto':not auto}[expected]
  results.append({'case':name,'expected':expected,'passed':passed,'items':checked,'seconds':round(time.time()-started,2)})
 report={'synthetic_only':True,'system_reminders_written':0,'usage':e.usage(),'results':results}
 Path(__file__).with_name('evaluation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'results':[{k:x[k] for k in ('case','expected','passed','seconds')} for x in results],'usage':report['usage']},ensure_ascii=False))
 if not all(x['passed'] for x in results):raise SystemExit(1)
