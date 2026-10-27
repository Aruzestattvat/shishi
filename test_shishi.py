import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from core import TZ, candidate, clean_message, validate_item, digest, review_batch, accepts
from server import Store, Engine
NOW=datetime(2026,10,21,12,tzinfo=TZ)

class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

_clock_patches = []
def setUpModule():
    for target in ('core.datetime', 'server.datetime'):
        clock = patch(target, FrozenDateTime)
        clock.start()
        _clock_patches.append(clock)

def tearDownModule():
    for clock in reversed(_clock_patches):
        clock.stop()


def msg(mid,text,who='1',delta=0,kind='text'):
    return {'id':mid,'text':text,'direction':who,'ts':int((NOW+timedelta(minutes=delta)).timestamp()*1000),'kind':kind,'truncated':False}
def item(messages,**kw):
    return dict({'title':'参加项目会议','kind':'event','owner':'me','confidence':.98,'due':'2026-10-22T15:00:00+08:00','time_uncertain':False,'action':'create','evidence':[{'id':m['id'],'quote':m['text']} for m in messages],'reason':'明确安排'},**kw)

class RuleTests(unittest.TestCase):
    def test_ok_bound_to_invite(self):
        ms=[msg('a','明天下午三点来开会，可以吗？'),msg('b','OK','2',1)]
        self.assertTrue(validate_item(item(ms),ms,NOW)['auto'])
    def test_accept_variants(self):
        for answer in ['好的','好','可以','行','没问题','同意','ok！']:
            with self.subTest(answer=answer):
                ms=[msg('a','明天下午三点开会'),msg('b',answer,'2',1)]
                self.assertTrue(candidate(ms[1]))
                self.assertTrue(validate_item(item(ms),ms,NOW)['auto'])
    def test_no_reply_is_review(self):
        ms=[msg('a','明天下午三点来开会，可以吗？')]
        self.assertFalse(validate_item(item(ms,owner='uncertain'),ms,NOW)['auto'])
    def test_extended_acceptance(self):
        for answer in ['okok，等我回来','好的，我这就去','可以呀，我马上','嗯嗯好','好嘞','行吧']:
            with self.subTest(answer=answer):
                self.assertTrue(accepts(answer))
                ms=[msg('a','去帮我买一份面包'),msg('b',answer,'2',1)]
                self.assertTrue(validate_item(item(ms,kind='task',due=None,title='帮家人买一份面包'),ms,NOW)['auto'])
    def test_explained_acceptance(self):
        for answer in ['可以的可以的，刚刚一直在忙没注意手机[捂脸]','可以的','好的好的，刚在忙','行行行，刚才没看到']:
            with self.subTest(answer=answer):
                self.assertTrue(accepts(answer))
    def test_course_with_followup_question_auto(self):
        invite=msg('a','本周课程安排：周五晚上7：00-9：00，周六下午1：00-3：00')
        follow=msg('f','孙老师，这周安排可以吗',delta=30)
        reply=msg('b','可以的可以的，刚刚一直在忙没注意手机[捂脸]','2',120)
        ms=[invite,follow,reply]
        out=validate_item(item([invite,reply],title='周五晚上7:00-9:00课程',kind='event',
            due='2026-10-23T19:00:00+08:00',time_uncertain=False,confidence=.95),ms,NOW)
        self.assertTrue(out['auto'],out['review_reasons'])
    def test_course_schedule_two_items_auto(self):
        invite=msg('a','本周课程安排：周五晚上7：00-9：00，周六下午1：00-3：00')
        reply=msg('b','可以的可以的，刚刚一直在忙没注意手机[捂脸]','2',120)
        ms=[invite,reply]
        for title,due in [('周五晚上7:00-9:00课程','2026-10-23T19:00:00+08:00'),
                          ('周六下午1:00-3:00课程','2026-10-24T13:00:00+08:00')]:
            with self.subTest(title=title):
                out=validate_item(item(ms,title=title,kind='event',due=due,time_uncertain=False,confidence=.95),ms,NOW)
                self.assertTrue(out['auto'],out['review_reasons'])
                self.assertNotIn('星期与提取日期不一致',out['review_reasons'])
                self.assertNotIn('原文有多个时刻，需要核对对应安排',out['review_reasons'])
    def test_extended_acceptance_rejects_new_topic(self):
        for answer in ['好的，另外帮我带个饭','ok，顺便拿个快递','行，但是我不想去']:
            with self.subTest(answer=answer):
                self.assertFalse(accepts(answer))
    def test_explicit_commit_without_clock_auto_adds_date_only(self):
        ms=[msg('a','朋友记得今晚和对方聊天'),msg('b','ok','2',1)]
        out=validate_item(item(ms,title='今晚和对方聊天',kind='event',due='2026-10-21T19:00:00+08:00',
            time_uncertain=True,confidence=.9),ms,NOW)
        self.assertTrue(out['auto'])
        self.assertEqual(out['due'],'2026-10-21')
        self.assertNotIn('存在时间歧义',out['review_reasons'])
    def test_festival_date_filled(self):
        ms=[msg('a','走万圣节去in77'),msg('b','OKOK','2',1)]
        out=validate_item(item(ms,title='万圣节去in77',kind='event',due=None,time_uncertain=True,confidence=.9),ms,NOW)
        self.assertTrue(out['auto'])
        self.assertEqual(out['due'],'2026-10-31')
    def test_okok_on_unlisted_invite_auto_adds(self):
        ms=[msg('a','走万圣节去in77'),msg('b','OKOK','2',1)]
        out=validate_item(item(ms,title='万圣节去in77',kind='event',due=None,time_uncertain=True,confidence=.9),ms,NOW)
        self.assertTrue(out['auto'])
        self.assertEqual(out['title'],'万圣节去in77')
        self.assertNotIn('需确认是否由你负责（自动加入要求你明确承诺）',out['review_reasons'])
    def test_title_keeps_time_prefix(self):
        ms=[msg('a','朋友记得今晚和对方聊天'),msg('b','ok','2',1)]
        out=validate_item(item(ms,title='和对方聊天',kind='event',due='2026-10-21T19:00:00+08:00',time_uncertain=True,confidence=.9),ms,NOW)
        self.assertEqual(out['title'],'今晚和对方聊天')
        self.assertTrue(out['auto'])
    def test_explicit_commit_without_date_auto_adds_undated(self):
        ms=[msg('a','记得和对方聊天'),msg('b','ok','2',1)]
        out=validate_item(item(ms,title='和对方聊天',kind='task',due=None,time_uncertain=True),ms,NOW)
        self.assertTrue(out['auto'])
        self.assertIsNone(out['due'])
    def test_ambiguous_clock_still_review(self):
        ms=[msg('a','今晚七点一起吃饭'),msg('b','ok','2',1)]
        out=validate_item(item(ms,title='一起吃饭',kind='event',due='2026-10-21T19:00:00+08:00',time_uncertain=True),ms,NOW)
        self.assertFalse(out['auto'])
        self.assertIn('存在时间歧义',out['review_reasons'])
    def test_received_not_acceptance(self):
        ms=[msg('a','明天下午三点来开会'),msg('b','收到','2',1)]
        self.assertFalse(validate_item(item(ms),ms,NOW)['auto'])
    def test_topic_switch(self):
        a=msg('a','明天下午三点开会');b=msg('x','给你推荐个电影',delta=1);c=msg('b','好的','2',2)
        self.assertFalse(validate_item(item([a,c]),[a,b,c],NOW)['auto'])
    def test_refusal_cannot_create(self):
        for answer in ['不去','不用了','没空','不要','拒绝']:
            ms=[msg('a','明天下午三点来开会'),msg('b',answer,'2',1)]
            with self.assertRaises(ValueError):validate_item(item(ms),ms,NOW)
    def test_commit(self):
        ms=[msg('a','我明天下午三点去参加会议','2')]
        self.assertTrue(validate_item(item(ms),ms,NOW)['auto'])
    def test_negated_commit(self):
        ms=[msg('a','我可能明天下午三点去参加会议','2')]
        self.assertFalse(validate_item(item(ms),ms,NOW)['auto'])
    def test_group_without_personal_reply(self):
        ms=[msg('a','明天下午三点大家开会')]
        self.assertFalse(validate_item(item(ms),ms,NOW,group=True)['auto'])
    def test_media_review(self):
        ms=[msg('a','我明天下午三点去参加会议','2',kind='voice')]
        self.assertFalse(validate_item(item(ms),ms,NOW)['auto'])
    def test_evidence_fake_id_and_quote(self):
        ms=[msg('a','明天来开会')]
        for evidence in [[{'id':'no','quote':'明天来开会'}],[{'id':'a','quote':'我肯定去'}]]:
            with self.assertRaises(ValueError):validate_item(item(ms,evidence=evidence),ms,NOW)
    def test_relative_date_anchor(self):
        ms=[msg('a','我明天下午三点去参加会议','2',delta=-1440)]
        r=validate_item(item(ms),ms,NOW)
        self.assertFalse(r['auto']);self.assertIn('相对日期与消息时间不一致',r['review_reasons'])
    def test_past_review(self):
        ms=[msg('a','我今天上午九点去参加会议','2')]
        self.assertFalse(validate_item(item(ms,due='2026-10-21T09:00:00+08:00'),ms,NOW)['auto'])
    def test_no_invented_time(self):
        ms=[msg('a','我去参加会议','2')]
        self.assertFalse(validate_item(item(ms),ms,NOW)['auto'])
    def test_undated_task(self):
        ms=[msg('a','我负责整理资料','2')]
        self.assertTrue(validate_item(item(ms,kind='task',due=None),ms,NOW)['auto'])
    def test_unknown_time_review(self):
        ms=[msg('a','我明天去参加会议','2')]
        self.assertFalse(validate_item(item(ms,due=None),ms,NOW)['auto'])
    def test_timezone_required(self):
        ms=[msg('a','我明天去参加会议','2')]
        result=validate_item(item(ms,due='2026-10-22T15:00:00'),ms,NOW)
        self.assertFalse(result['auto']);self.assertIsNone(result['due'])
    def test_wrong_hour_review(self):
        ms=[msg('a','我明天下午三点去参加会议','2')]
        self.assertFalse(validate_item(item(ms,due='2026-10-22T09:00:00+08:00'),ms,NOW)['auto'])
    def test_ambiguous_hour_review(self):
        ms=[msg('a','我明天三点去参加会议','2')]
        self.assertFalse(validate_item(item(ms),ms,NOW)['auto'])
    def test_all_day(self):
        ms=[msg('a','我明天去参加会议','2')]
        self.assertTrue(validate_item(item(ms,due='2026-10-22'),ms,NOW)['auto'])
    def test_no_system_instruction(self):
        self.assertFalse(candidate(msg('a','提醒明天做事','0')))
    def test_xml(self):
        m=clean_message({'id':'x','text':'<msg><title>明天会议</title><url>http://example.invalid</url></msg>','kind':'xml'})
        self.assertEqual(m['text'],'明天会议')
    def test_malformed_xml(self):
        self.assertEqual(clean_message({'text':'<msg>'})['text'],'')
    def test_changes_review(self):
        ms=[msg('a','我明天去参加会议','2')]
        for a in ['cancel','reschedule','complete']:
            self.assertFalse(validate_item(item(ms,action=a),ms,NOW)['auto'])
    def test_confidence_boolean(self):
        ms=[msg('a','我明天去参加会议','2')]
        with self.assertRaises(ValueError):validate_item(item(ms,confidence=True),ms,NOW)


class ReplyTests(unittest.TestCase):
    def test_unanswered_fallback(self):
        ms=[msg('a','明天来开会，可以吗？')]
        out=review_batch([],ms,['a'],now=NOW)
        self.assertEqual(len(out),1);self.assertFalse(out[0]['auto'])
    def test_others_plan_no_fallback(self):
        ms=[msg('a','我明天参加会议')]
        self.assertEqual(review_batch([],ms,['a'],now=NOW),[])
    def test_refusal_overrides_model_missed_refusal(self):
        ms=[msg('a','明天来开会'),msg('b','不去','2',1)]
        out=review_batch([item([ms[0]],owner='uncertain')],ms,['a','b'],now=NOW)
        self.assertEqual(out,[])
    def test_model_empty_still_dismisses_pending(self):
        ms=[msg('a','明天来开会'),msg('b','不去','2',1)]
        old=dict(validate_item(item([ms[0]],owner='uncertain'),[ms[0]],NOW),id='old',status='review')
        out=review_batch([],ms,['b'],[old],now=NOW)
        self.assertEqual(out[0]['action'],'dismiss')
    def test_model_empty_still_confirms_pending(self):
        ms=[msg('a','明天下午三点来开会'),msg('b','好的','2',1)]
        old=dict(validate_item(item([ms[0]],owner='uncertain'),[ms[0]],NOW),id='old',status='review')
        out=review_batch([],ms,['b'],[old],now=NOW)
        self.assertEqual(out[0]['action'],'confirm');self.assertTrue(out[0]['auto'])
    def test_multi_invite_requires_review(self):
        ms=[msg('a','明天下午三点来开会，顺便帮我交资料'),msg('b','OK','2',1)]
        self.assertFalse(validate_item(item(ms),ms,NOW)['auto'])
    def test_low_confidence_clear_reply(self):
        ms=[msg('a','明天下午三点来开会'),msg('b','OK','2',1)]
        self.assertTrue(validate_item(item(ms,confidence=.90),ms,NOW)['auto'])
    def test_late_decline_dismisses_pending(self):
        invite=msg('a','周日一起去吃龙哥小火锅');reply=msg('b','那不行，我又不在家','2',1)
        old=dict(validate_item(item([invite],owner='uncertain'),[invite],NOW),id='old',status='review')
        out=review_batch([],[invite,reply],['b'],[old],now=NOW)
        self.assertEqual(out[0]['action'],'dismiss')
    def test_extended_reply_confirms_pending(self):
        invite=msg('a','去帮我买一份面包');reply=msg('b','okok，等我回来','2',1)
        old=dict(validate_item(item([invite],owner='uncertain',kind='task',due=None), [invite],NOW),id='old',status='review')
        out=review_batch([],[invite,reply],['b'],[old],now=NOW)
        self.assertEqual(out[0]['action'],'confirm');self.assertTrue(out[0]['auto'])
    def test_wrong_fresh_id_skipped(self):
        ms=[msg('a','明天下午三点来开会')]
        skipped=[]
        self.assertEqual(review_batch([item(ms)],ms,['unknown'],now=NOW,on_skip=skipped.append),[])
        self.assertEqual(len(skipped),1)


class JudgmentRegressionTests(unittest.TestCase):
    def pair(self, answer, request='去帮我买一份面包'):
        return [msg('a',request),msg('b',answer,'2',1)]
    def test_hesitation_never_auto(self):
        for answer in ['好的，我考虑一下','好的，我不一定去','好的，我尽量去','好的，我再看看']:
            ms=self.pair(answer)
            self.assertFalse(validate_item(item(ms,kind='task',due=None),ms,NOW)['auto'])
    def test_takeover_preserves_request_and_reply(self):
        ms=self.pair('好的，不用你买，我自己买')
        out=validate_item(item(ms,kind='task',due=None),ms,NOW)
        self.assertTrue(out['auto']);self.assertEqual([e['text'] for e in out['evidence']],[m['text'] for m in ms])
    def test_takeover_does_not_swallow_later_refusal(self):
        ms=self.pair('好的，不用你买，我自己买，算了不买了')
        with self.assertRaises(ValueError):validate_item(item(ms,kind='task',due=None),ms,NOW)
    def test_later_refusal_overrides_partial_model_evidence(self):
        ms=self.pair('OK')+[msg('c','算了，我不去了','2',2)]
        self.assertEqual(review_batch([item(ms[:2],kind='task',due=None)],ms,['a','b','c'],now=NOW),[])
    def test_later_hesitation_overrides_partial_model_evidence(self):
        ms=self.pair('OK')+[msg('c','我再考虑一下','2',2)]
        self.assertFalse(validate_item(item(ms[:2],kind='task',due=None),ms,NOW)['auto'])
    def test_pending_latest_refusal_wins(self):
        ms=self.pair('OK')+[msg('c','算了，我不去了','2',2)]
        old=dict(validate_item(item(ms[:1],kind='task',due=None,owner='uncertain'),ms[:1],NOW),id='old',status='review')
        out=review_batch([],ms,['b','c'],[old],now=NOW)
        self.assertEqual([x['action'] for x in out],['dismiss'])
        self.assertEqual(out[0]['evidence'][-1]['id'],'c')
    def test_topic_switch_refusal_not_bound_to_old_request(self):
        ms=self.pair('OK')+[msg('x','另外要不要看电影',delta=2),msg('c','不去','2',3)]
        out=validate_item(item(ms[:2],kind='task',due=None),ms,NOW)
        self.assertTrue(out['auto'])
    def test_explicit_this_year_festival_not_rolled_forward(self):
        ms=self.pair('OK','今年国庆节一起吃饭')
        out=validate_item(item(ms,due=None),ms,NOW)
        self.assertEqual(out['due'],'2026-10-01');self.assertFalse(out['auto'])
    def test_wrong_model_festival_year_blocked(self):
        ms=self.pair('OK','今年国庆节一起吃饭')
        self.assertFalse(validate_item(item(ms,due='2027-10-01'),ms,NOW)['auto'])
    def test_festival_anchor_is_message_date(self):
        ms=[msg('a','元旦一起吃饭'),msg('b','OK','2',1)]
        for m in ms:m['ts']=int(datetime(2026,12,31,12,tzinfo=TZ).timestamp()*1000)+(60000 if m['id']=='b' else 0)
        out=validate_item(item(ms,due=None),ms,datetime(2027,1,2,12,tzinfo=TZ))
        self.assertIsNone(out['due']);self.assertFalse(out['auto'])
    def test_explicit_next_year_festival(self):
        ms=self.pair('OK','明年国庆节一起吃饭')
        out=validate_item(item(ms,due=None),ms,NOW)
        self.assertEqual(out['due'],'2027-10-01');self.assertTrue(out['auto'])

class ExpandedRegressionTests(unittest.TestCase):
    def pair(self, answer, request='帮我买面包'):
        return [msg('a',request),msg('b',answer,'2',1)]
    def checked(self, ms, refs=None, **kw):
        return validate_item(item(refs or ms,kind='task',due=None,**kw),ms,NOW)
    def test_new_uncommitted_variants(self):
        for text in ['好的，我还没决定','好的，我暂时答应不了','好的，我只是说说','好的，我已经买完了','好的，我让他去买']:
            with self.subTest(text=text):self.assertFalse(self.checked(self.pair(text))['auto'])
    def test_negation_subject_and_double_negative(self):
        for text in ['不是不去，我会去','不用你买了，我自己去买','我不会不去的','他不去，我去']:
            with self.subTest(text=text):self.assertTrue(self.checked(self.pair(text))['auto'])
    def test_conditions_and_quotes_never_dismiss(self):
        for text in ['如果没空我就不去','他说不用了，我还没确认','“不去”是他说的','要是没空我就不去']:
            ms=self.pair(text)
            old=dict(self.checked(ms[:1],owner='uncertain'),id='old',status='review')
            out=review_batch([],ms,['b'],[old],now=NOW)
            self.assertFalse(any(x['action']=='dismiss' or x['auto'] for x in out))
    def test_correction_both_directions(self):
        for first,last,expected in [('不去','我改主意了，我会去',True),('好的，我考虑一下','我决定了，我去',True),('OK','我再考虑一下',False)]:
            ms=self.pair(first)+[msg('c',last,'2',2)]
            out=self.checked(ms)
            self.assertEqual(out['auto'],expected)
    def test_correction_evidence_includes_latest_reply(self):
        ms=self.pair('好的，我考虑一下')+[msg('c','我决定了，我去','2',2)]
        out=self.checked(ms,ms[:2])
        self.assertTrue(out['auto']);self.assertEqual(out['evidence'][-1]['id'],'c')
    def test_topic_change_not_treated_as_reversal(self):
        ms=self.pair('不去')+[msg('c','另外我会去拿快递','2',2)]
        with self.assertRaises(ValueError):self.checked(ms,ms[:2])
    def test_pending_reversal_updates_only_existing_item(self):
        ms=self.pair('不去')+[msg('c','我改主意了，我会去','2',2)]
        old=dict(self.checked(ms[:1],owner='uncertain'),id='old',status='review')
        out=review_batch([],ms,['b','c'],[old],now=NOW)
        self.assertEqual(len(out),1);self.assertEqual(out[0]['target_id'],'old');self.assertTrue(out[0]['auto'])
    def test_weekday_and_week_offset(self):
        for wording,due,expected in [('周日','2026-10-25',True),('周日','2026-10-26',False),('下周一','2026-10-26',True),('下周一','2026-11-02',False),('下下周一','2026-11-02',True),('本周五','2026-10-30',False)]:
            ms=self.pair('OK',wording+'买面包')
            out=validate_item(item(ms,kind='task',due=due),ms,NOW)
            self.assertEqual(out['auto'],expected,(wording,due))
    def test_later_media_reply_still_requires_review(self):
        ms=self.pair('我考虑一下')+[msg('c','我决定了，我去','2',2,kind='voice')]
        out=self.checked(ms,ms[:2]);self.assertFalse(out['auto'])

class ReverseAgreementTests(unittest.TestCase):
    def pair(self, answer='可以的', proposal='明天出来学习'):
        return [msg('a',proposal,'2'),msg('b',answer,'1',1)]
    def checked(self, ms, **kw):
        values={'title':'一起学习','due':'2026-10-22','confidence':.95}
        values.update(kw)
        return validate_item(item(ms,**values),ms,NOW)
    def test_explicit_partner_confirmation(self):
        for answer in ['可以的','好的','OK','没问题']:
            with self.subTest(answer=answer):self.assertTrue(self.checked(self.pair(answer))['auto'])
    def test_acknowledgment_is_not_agreement(self):
        for answer in ['奥','哦','收到','知道了','可以吗','可以，不过我得看看']:
            with self.subTest(answer=answer):
                out=self.checked(self.pair(answer));self.assertFalse(out['auto'])
                self.assertIn('你已提出安排，尚缺少对方明确确认',out['review_reasons'])
    def test_polite_negotiation_and_afternoon(self):
        ms=self.pair('可以的','你好，今天下午可能得推迟半小时，一点半开始可以吗[合十]')
        out=self.checked(ms,due='2026-10-21T13:30:00+08:00')
        self.assertTrue(out['auto'],out['review_reasons'])
    def test_reschedule_still_has_explicit_change_gate(self):
        ms=self.pair('可以的','今天下午推迟到一点半开始可以吗')
        out=self.checked(ms,due='2026-10-21T13:30:00+08:00',action='reschedule',target_id='original')
        self.assertFalse(out['auto'])
        self.assertEqual(out['review_reasons'],['变更或完成操作需要确认，以免误改已有提醒'])
    def test_wrong_afternoon_clock_blocked(self):
        ms=self.pair('可以的','明天下午一起学习，一点半开始可以吗')
        out=self.checked(ms,due='2026-10-22T01:30:00+08:00')
        self.assertFalse(out['auto']);self.assertIn('提取时刻与原文不一致',out['review_reasons'])
    def test_conditions_not_removed_by_partner_ok(self):
        for proposal in ['如果有空明天出来学习','我还没决定，明天出来学习可以吗','明天可能一起学习可以吗','明天让他去开会','明天你帮我买面包','明天一起学习或者吃饭可以吗']:
            with self.subTest(proposal=proposal):self.assertFalse(self.checked(self.pair(proposal=proposal))['auto'])
    def test_no_reply_not_auto(self):
        self.assertFalse(self.checked(self.pair()[:1])['auto'])
    def test_group_confirmation_not_personal(self):
        ms=self.pair()
        self.assertFalse(validate_item(item(ms,due='2026-10-22'),ms,NOW,group=True)['auto'])
    def test_intervening_topic_not_bound(self):
        ms=[self.pair()[0],msg('x','照片看到了吗','2',1),msg('b','好的','1',2)]
        self.assertFalse(self.checked(ms)['auto'])
    def test_later_correction_not_ignored(self):
        ms=self.pair()+[msg('c','算了，改天吧','1',2)]
        self.assertFalse(self.checked(ms)['auto'])
    def test_other_owner_never_promoted(self):
        self.assertFalse(self.checked(self.pair(),owner='other')['auto'])
    def test_model_empty_confirms_existing_proposal(self):
        ms=self.pair();old=dict(self.checked(ms[:1]),id='old',status='review')
        out=review_batch([],ms,['b'],[old],now=NOW)
        self.assertEqual(len(out),1);self.assertTrue(out[0]['auto']);self.assertEqual(out[0]['target_id'],'old')
    def test_model_new_date_not_overwritten_by_local_fallback(self):
        ms=self.pair('可以的','明天下午推迟到三点开始可以吗')
        old=dict(self.checked(ms[:1],due='2026-10-22T14:00:00+08:00'),id='old',status='review')
        change=item(ms,action='reschedule',target_id='old',due='2026-10-22T15:00:00+08:00')
        out=review_batch([change],ms,['b'],[old],now=NOW)
        self.assertEqual(len(out),1);self.assertEqual(out[0]['due'],change['due'])
    def test_different_day_does_not_inherit_afternoon(self):
        ms=[msg('a','我明天下午上课，后天一点开会','2')]
        out=self.checked(ms,due='2026-10-23T13:00:00+08:00')
        self.assertFalse(out['auto']);self.assertIn('上午或下午尚未明确',out['review_reasons'])

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Store(pathlib.Path(self.tmp.name)/'test.db')
        self.ms=[msg('a','明天下午三点来开会')]
        self.i=validate_item(item(self.ms,owner='uncertain'),self.ms,NOW)
    def tearDown(self):self.tmp.cleanup()
    def test_duplicate_idempotent(self):
        self.db.put_item('s',self.i);self.db.put_item('s',self.i)
        self.assertEqual(len(self.db.items()),1)
    def test_sessions_isolated(self):
        self.db.put_item('s1',self.i);self.db.put_item('s2',self.i)
        self.assertEqual(len(self.db.items()),2)
    def test_confirm_updates_pending(self):
        self.db.put_item('s',self.i);old=self.db.items()[0]
        ms=self.ms+[msg('b','好的','2',1)]
        update=validate_item(item(ms,action='confirm',target_id=old['id']),ms,NOW)
        self.db.put_item('s',update)
        self.assertEqual(len(self.db.items()),1)
        self.assertEqual(self.db.items()[0]['id'],old['id'])
    def test_refusal_dismisses_pending(self):
        self.db.put_item('s',self.i);old=self.db.items()[0]
        ms=self.ms+[msg('b','不去','2',1)]
        update=validate_item(item(ms,action='dismiss',target_id=old['id'],owner='uncertain'),ms,NOW)
        self.db.put_item('s',update)
        self.assertEqual(self.db.items()[0]['status'],'dismissed')
    def test_latest_refusal_saved_with_full_evidence(self):
        self.db.put_item('s',self.i)
        ms=self.ms+[msg('b','OK','2',1),msg('c','算了，我不去了','2',2)]
        out=review_batch([],ms,['b','c'],self.db.items('s'),now=NOW)
        for decision in out:self.db.put_item('s',decision)
        saved=self.db.items('s')[0]
        self.assertEqual(saved['status'],'dismissed')
        self.assertEqual(saved['evidence'][-1]['text'],'算了，我不去了')
    def test_takeover_pending_updates_same_item(self):
        ms=[msg('a','帮我买肉饼'),msg('b','好的，不用你买，我自己买','2',1)]
        old=validate_item(item(ms[:1],kind='task',due=None,owner='uncertain'),ms[:1],NOW)
        self.db.put_item('s',old)
        original=self.db.items('s')[0]['id']
        for decision in review_batch([],ms,['b'],self.db.items('s'),now=NOW):self.db.put_item('s',decision)
        saved=self.db.items('s')
        self.assertEqual(len(saved),1);self.assertEqual(saved[0]['id'],original)
        self.assertEqual(saved[0]['status'],'ready')
    def test_fake_dismiss_does_not_remove(self):
        self.db.put_item('s',self.i);old=self.db.items()[0]
        update=validate_item(item(self.ms,action='dismiss',target_id=old['id']),self.ms,NOW)
        self.db.put_item('s',update)
        self.assertEqual(self.db.items()[0]['status'],'review')
    def test_cross_chat_target(self):
        self.db.put_item('a',self.i);old=self.db.items()[0]
        update=dict(self.i,action='confirm',target_id=old['id'],auto=True)
        self.db.put_item('b',update)
        self.assertEqual(self.db.items('a')[0]['status'],'review')
        self.assertEqual(self.db.items('b')[0]['status'],'review')
    def test_overlapping_evidence_different_due_not_duplicate(self):
        self.db.put_item('s',self.i)
        other=dict(self.i,title='另一场不同的安排',due='2026-10-23T15:00:00+08:00',auto=True)
        self.db.put_item('s',other)
        saved=[x for x in self.db.items() if x['id']!=self.db.items()[0]['id']]
        self.assertFalse(any('与已有事项证据重叠' in (x.get('review_reasons') or []) for x in saved))
    def test_overlapping_evidence_review(self):
        self.db.put_item('s',self.i)
        self.db.put_item('s',dict(self.i,title='参加同一场会议',auto=True))
        self.assertTrue(all(x['status']=='review' for x in self.db.items()))

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        with patch('server.credentials',return_value={}):self.e=Engine(self.tmp.name)
        self.e.state['account_ok']=True
        self.e.store.execute('INSERT INTO sessions(id,name,kind,last_ts,selected,synced_ts) VALUES(?,?,?,?,?,?)',('s','测试',1,1,1,1))
    def tearDown(self):self.tmp.cleanup()
    def insert(self,m,processed=0):
        self.e.store.execute('INSERT INTO messages(sid,mid,ts,body,hash,processed) VALUES(?,?,?,?,?,?)',('s',m['id'],m['ts'],json.dumps(m),digest(m),processed))
    def test_unselected_no_model(self):
        self.insert(msg('a','明天开会'))
        self.e.store.execute('UPDATE sessions SET selected=0')
        with patch.object(self.e,'extract') as llm:self.assertFalse(self.e.analyze_once());llm.assert_not_called()
    def test_chitchat_zero_model(self):
        self.insert(msg('a','哈哈哈哈'))
        with patch.object(self.e,'extract') as llm:self.assertFalse(self.e.analyze_once());llm.assert_not_called()
        self.assertEqual(self.e.store.query('SELECT processed FROM messages')[0]['processed'],1)
    def test_duplicate_does_not_reanalyze(self):
        m=msg('a','明天开会');self.insert(m)
        with patch.object(self.e,'extract',return_value={'items':[]}) as llm:
            self.assertTrue(self.e.analyze_once());self.assertFalse(self.e.analyze_once());self.assertEqual(llm.call_count,1)
    def test_selection_change_discards_inflight(self):
        ms=[msg('a','明天开会')];self.insert(ms[0])
        def result(*args):
            self.e.store.execute('UPDATE sessions SET selected=0');return {'items':[item(ms,owner='uncertain')]}
        with patch.object(self.e,'extract',side_effect=result):self.e.analyze_once()
        self.assertEqual(self.e.store.items(),[])
    def test_failure_keeps_pending(self):
        self.insert(msg('a','明天开会'))
        with patch.object(self.e,'extract',side_effect=ValueError('failure')):
            with self.assertRaises(ValueError):self.e.analyze_once()
        self.assertEqual(self.e.store.query('SELECT processed FROM messages')[0]['processed'],0)
    def test_sync_not_complete_waits(self):
        self.insert(msg('a','明天开会'));self.e.store.execute('UPDATE sessions SET synced_ts=0')
        with patch.object(self.e,'extract') as llm:self.assertFalse(self.e.analyze_once());llm.assert_not_called()
    def test_invalid_evidence_not_committed(self):
        ms=[msg('a','明天下午三点见')];self.insert(ms[0])
        with patch.object(self.e,'extract',return_value={'items':[item(ms,evidence=[{'id':'a','quote':'伪造证据'}])]}):
            self.assertTrue(self.e.analyze_once())
        self.assertEqual(self.e.store.items(),[])
        self.assertEqual(self.e.store.query('SELECT processed FROM messages')[0]['processed'],1)
        self.assertIn('已跳过',self.e.state.get('notice',''))
    def test_discover_stores_pinned_and_official_flags(self):
        def fake(path):
            if path.startswith('/api/v1/accounts'):return {'selected_account_id':'acct'}
            return {'items':[
                {'id':'p1','display_name':'置顶群','kind':2,'last_activity_unix_ms':1792567016000,'avatar_uri':'',
                 'is_pinned':True,'is_muted':True,'is_folded':False,'sort_timestamp':1792567016},
                {'id':'off1','display_name':'公众号A','kind':3,'last_activity_unix_ms':1792550000000,'avatar_uri':'https://x/y',
                 'is_pinned':False,'is_muted':False,'is_folded':True,'sort_timestamp':1792550000}],'hasMore':False}
        with patch.object(self.e,'alpha',side_effect=fake):self.e.discover()
        rows={r['id']:r for r in self.e.store.query('SELECT * FROM sessions')}
        self.assertEqual((rows['p1']['pinned'],rows['p1']['muted'],rows['p1']['sort_ts']),(1,1,1792567016))
        self.assertEqual((rows['off1']['kind'],rows['off1']['folded'],rows['off1']['avatar']),(3,1,'https://x/y'))

    def test_sync_message_insert_after_schema_migration(self):
        m=msg('sync1','明天下午三点开会')
        raw={'platform_message_id':'sync1','sent_at_unix_ms':m['ts'],'direction':'1','kind':'text','text':'明天下午三点开会'}
        session={'id':'s','offset':0,'last_ts':m['ts']}
        with patch.object(self.e,'alpha',return_value={'messages':[raw],'hasMore':False}):
            self.e.sync_session(session)
        rows=self.e.store.query('SELECT mid,processed FROM messages')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['mid'],'sync1');self.assertEqual(rows[0]['processed'],0)
        self.assertEqual(self.e.store.query('SELECT offset FROM sessions WHERE id=?',('s',))[0]['offset'],1)

    def test_budget_blocks_before_network(self):
        self.e.store.set_setting('daily_budget',10)
        with patch('urllib.request.urlopen') as url:
            with self.assertRaisesRegex(ValueError,'预算'):self.e.extract([msg('a','明天开会')],['a'],[],{'kind':1})
            url.assert_not_called()
    def test_untranscribed_voice_waits_for_asr(self):
        m=msg('v1','[语音]');body=dict(m,kind='voice')
        self.e.asr_enabled=True
        self.e.store.execute('INSERT INTO messages(sid,mid,ts,body,hash,processed,transcribed) VALUES(?,?,?,?,?,0,0)',
            ('s','v1',m['ts'],json.dumps(body),digest(body)))
        with patch.object(self.e,'extract') as llm:
            self.assertFalse(self.e.analyze_once());llm.assert_not_called()
        self.assertEqual(self.e.store.query("SELECT processed FROM messages WHERE mid='v1'")[0]['processed'],0)
    def test_transcribed_voice_is_analyzed(self):
        m=msg('v3','你明天下午三点来开会吗');body=dict(m,kind='voice',transcribed=True)
        self.e.asr_enabled=True
        self.e.store.execute('INSERT INTO messages(sid,mid,ts,body,hash,processed,transcribed) VALUES(?,?,?,?,?,0,1)',
            ('s','v3',m['ts'],json.dumps(body,ensure_ascii=False),digest(body)))
        with patch.object(self.e,'extract',return_value={'items':[]}) as llm:
            self.assertTrue(self.e.analyze_once())
        self.assertEqual(llm.call_count,1)
        self.assertEqual(self.e.store.query("SELECT processed FROM messages WHERE mid='v3'")[0]['processed'],1)
    def test_voice_transcription_updates_body(self):
        m=msg('v2','[语音]');body=dict(m,kind='voice')
        self.e.asr_enabled=True
        self.e.store.execute('INSERT INTO messages(sid,mid,ts,body,hash,processed,transcribed,seq) VALUES(?,?,?,?,?,0,0,?)',
            ('s','v2',m['ts'],json.dumps(body),digest(body),42))
        row=self.e.store.query("SELECT * FROM messages WHERE mid='v2'")[0]
        def fake_run(args,**kw):
            import types
            return types.SimpleNamespace(stdout=json.dumps({p:'帮我拿个快递' for p in args[2:]}).encode())
        with patch.object(self.e,'media_url',return_value='/api/v1/media/x'),             patch.object(self.e,'alpha_bytes',return_value=b'RIFFxxxx'),             patch('server.subprocess.run',side_effect=fake_run):
            self.assertEqual(self.e.transcribe_voices([row]),1)
        saved=self.e.store.query("SELECT * FROM messages WHERE mid='v2'")[0]
        saved_body=json.loads(saved['body'])
        self.assertEqual(saved_body['text'],'帮我拿个快递')
        self.assertTrue(saved_body['transcribed'])
        self.assertEqual(saved['transcribed'],1)
        self.assertEqual(saved['processed'],0)
    def test_menu_snapshot_only_pending(self):
        now=int(NOW.timestamp()*1000)
        def put(iid,status,ts,title):
            body=json.dumps({'title':title,'evidence':[{'id':'m','ts':ts,'quote':'x','direction':'1','kind':'text'}]},ensure_ascii=False)
            self.e.store.execute("INSERT INTO items(id,sid,body,status,revision,updated) VALUES(?,?,?,?,?,?)",(iid,'s',body,status,1,1.0))
        put('review-now','review',now,'待确认事项')
        put('synced-now','synced',now,'待办事项')
        put('done-now','completed',now,'已完成事项')
        put('review-old','review',now-10*86400*1000,'旧待确认')
        snap=self.e.menu_snapshot()
        self.assertEqual(snap['pending'],1)
        self.assertEqual((snap['review'],snap['active']),(1,0))
        self.assertEqual([x['id'] for x in snap['items']],['review-now'])
        self.e.write_menu()
        saved=json.loads((self.e.ipc/'menu.json').read_text())
        self.assertEqual(saved['pending'],1)

    def test_item_toggle_complete_and_reopen(self):
        body=json.dumps({'title':'帮家人买一份面包','evidence':[],'action':'create'})
        self.e.store.execute("INSERT INTO items(id,sid,body,status,reminder_id,revision,updated) VALUES(?,?,?,?,?,?,?)",
            ('i1','s',body,'synced','r1',3,1.0))
        self.e.action('/api/item',{'id':'i1','action':'complete'})
        row=self.e.store.query("SELECT status,body,revision,reminder_id FROM items WHERE id='i1'")[0]
        self.assertEqual(row['status'],'ready')
        self.assertEqual(json.loads(row['body'])['action'],'complete')
        self.assertEqual(json.loads(row['body'])['target_id'],'i1')
        self.assertGreater(json.loads(row['body'])['manual_at'],0)
        self.assertEqual(row['revision'],4)
        self.e.store.execute("UPDATE items SET status='completed' WHERE id='i1'")
        self.e.action('/api/item',{'id':'i1','action':'reopen'})
        row=self.e.store.query("SELECT status,body FROM items WHERE id='i1'")[0]
        self.assertEqual(row['status'],'ready')
        self.assertEqual(json.loads(row['body'])['action'],'reopen')
    def test_item_toggle_rejects_review(self):
        body=json.dumps({'title':'测试','evidence':[],'action':'create'})
        self.e.store.execute("INSERT INTO items(id,sid,body,status,revision,updated) VALUES(?,?,?,?,?,?)",
            ('i2','s',body,'review',1,1.0))
        with self.assertRaises(ValueError):self.e.action('/api/item',{'id':'i2','action':'complete'})
    def test_command_idempotent(self):
        a=self.e.command('save',{'title':'测试'},'abc',1);b=self.e.command('save',{'title':'测试'},'abc',1)
        self.assertEqual(a,b);self.assertEqual(len(self.e.store.query('SELECT * FROM commands')),1)
    def test_bad_action(self):
        with self.assertRaises(ValueError):self.e.action('/api/native',{'op':'erase'})
    def test_budget_validation(self):
        with self.assertRaises(ValueError):self.e.action('/api/settings',{'budget':1})

class PipelineTests(unittest.TestCase):
    """回归：整批分析、回看范围、单条脏数据与错误分级。"""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        with patch('server.credentials',return_value={}):self.e=Engine(self.tmp.name)
        self.e.state['account_ok']=True
        self.e.store.execute('INSERT INTO sessions(id,name,kind,last_ts,selected,synced_ts) VALUES(?,?,?,?,?,?)',('s','测试',1,1,1,1))
    def tearDown(self):self.tmp.cleanup()
    def insert(self,m,processed=0):
        self.e.store.execute('INSERT INTO messages(sid,mid,ts,body,hash,processed) VALUES(?,?,?,?,?,?)',('s',m['id'],m['ts'],json.dumps(m),digest(m),processed))
    def test_invalid_date_retains_own_commitment_for_review(self):
        ms=[msg('a','我明天下午三点去参加会议','2')]
        self.insert(ms[0])
        for bad_date in ['日期格式错误','2026-02-30',{'date':'tomorrow'}]:
            with self.subTest(bad_date=bad_date):
                self.e.store.execute('UPDATE messages SET processed=0')
                self.e.store.execute('DELETE FROM items')
                with patch.object(self.e,'extract',return_value={'items':[item(ms,due=bad_date)]}):
                    self.assertTrue(self.e.analyze_once())
                saved=self.e.store.items()
                self.assertEqual(len(saved),1)
                self.assertEqual(saved[0]['status'],'review')
                self.assertIsNone(saved[0]['due'])
                self.assertEqual(saved[0]['evidence'][0]['ts'],ms[0]['ts'])
                self.assertEqual(saved[0]['evidence'][0]['quote'],ms[0]['text'])
                self.assertTrue(any('日期解析失败' in r for r in saved[0]['review_reasons']))

    def test_reply_after_review_is_analyzed(self):
        invite=msg('a','周日一起去吃龙哥小火锅');self.insert(invite,processed=1)
        old=dict(validate_item(item([invite],owner='uncertain'),[invite],NOW),id='old',status='review')
        self.e.store.put_item('s',old)
        reply=msg('b','那行','2',2);self.assertFalse(candidate(reply));self.insert(reply)
        with patch.object(self.e,'extract',return_value={'items':[]}) as llm:
            self.assertTrue(self.e.analyze_once())
        self.assertEqual(llm.call_count,1)
        row=[r for r in self.e.store.query('SELECT mid,processed FROM messages') if r['mid']=='b'][0]
        self.assertEqual(row['processed'],1)
    def test_many_messages_one_call(self):
        for i in range(10):self.insert(msg('m%d'%i,'明天下午三点开会%d'%i,delta=i))
        with patch.object(self.e,'extract',return_value={'items':[]}) as llm:
            self.assertTrue(self.e.analyze_once())
        self.assertEqual(llm.call_count,1)
        self.assertEqual(self.e.store.query('SELECT count(*) n FROM messages WHERE processed=1')[0]['n'],10)
    def test_old_messages_skipped_by_lookback(self):
        old=msg('old','明天下午三点开会');old['ts']=int((NOW-timedelta(days=60)).timestamp()*1000)
        self.insert(old);self.insert(msg('new','明天下午三点开会',delta=-1))
        with patch.object(self.e,'extract',return_value={'items':[]}):self.e.analyze_once()
        rows={r['mid']:r for r in self.e.store.query('SELECT mid,processed,skipped FROM messages')}
        self.assertEqual((rows['old']['processed'],rows['old']['skipped']),(1,1))
        self.assertEqual(rows['new']['processed'],1)
        with patch('server.credentials',return_value={}):
            self.e.action('/api/settings',{'new_only':False})
            self.e.action('/api/settings',{'lookback_days':365})
        rows={r['mid']:r for r in self.e.store.query('SELECT mid,processed,skipped FROM messages')}
        self.assertEqual((rows['old']['processed'],rows['old']['skipped']),(0,0))
    def test_bad_model_item_does_not_lose_good_item(self):
        ms=[msg('a','明天下午三点来开会'),msg('b','好的','2',1)]
        self.insert(ms[0]);self.insert(ms[1])
        good=item(ms);bad=dict(good,evidence=[{'id':'a','quote':'伪造的证据'}])
        with patch.object(self.e,'extract',return_value={'items':[bad,good]}):self.e.analyze_once()
        titles=[x['title'] for x in self.e.store.items()]
        self.assertEqual(titles,['明天参加项目会议'])
    def test_new_only_skips_old_messages(self):
        old=msg('old','明天下午三点开会');old['ts']=int((NOW-timedelta(days=2)).timestamp()*1000)
        self.insert(old);self.insert(msg('new','明天下午三点开会',delta=-1))
        with patch.object(self.e,'extract',return_value={'items':[]}):self.e.analyze_once()
        rows={r['mid']:r for r in self.e.store.query('SELECT mid,processed,skipped FROM messages')}
        self.assertEqual((rows['old']['processed'],rows['old']['skipped']),(1,1))
        self.assertEqual(rows['new']['processed'],1)
    def test_snapshot_exposes_effective_cutoff(self):
        snap=self.e.snapshot()
        self.assertTrue(snap['new_only'])
        self.assertIn('cutoff',snap)
        self.assertGreater(snap['cutoff'],0)
        self.assertGreaterEqual(snap['older'],0)

    def test_since_date_validation(self):
        with self.assertRaises(ValueError):self.e.action('/api/settings',{'since_date':'2999-01-01'})
        with self.assertRaises(ValueError):self.e.action('/api/settings',{'since_date':'bad'})
    def test_since_date_change_restores_skipped(self):
        old=msg('old2','明天下午三点开会');old['ts']=int((NOW-timedelta(days=5)).timestamp()*1000)
        self.insert(old)
        with patch.object(self.e,'extract',return_value={'items':[]}):self.e.analyze_once()
        self.assertEqual(self.e.store.query('SELECT skipped FROM messages')[0]['skipped'],1)
        with patch('server.credentials',return_value={}):
            self.e.action('/api/settings',{'new_only':True,'since_date':(NOW-timedelta(days=10)).strftime('%Y-%m-%d')})
        row=self.e.store.query('SELECT processed,skipped FROM messages')[0]
        self.assertEqual((row['processed'],row['skipped']),(0,0))

    def test_error_classification(self):
        import urllib.error
        self.assertTrue(Engine.fatal_error(urllib.error.HTTPError('u',402,'quota',{},None),'API 额度不足'))
        self.assertFalse(Engine.fatal_error(TimeoutError('slow'),'网络连接失败，稍后自动重试'))
        self.assertTrue(Engine.fatal_error(ValueError('今日 token 预算不足'),'今日 token 预算不足'))

class AvatarTests(unittest.TestCase):
    """头像按会话缓存：只下载一次，识别内容类型，缺失时返回空。"""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        with patch('server.credentials',return_value={}):self.e=Engine(self.tmp.name)
        self.e.store.execute('INSERT INTO sessions(id,name,kind,last_ts,selected,synced_ts) VALUES(?,?,?,?,?,?)',('s','测试',1,1,1,1))
    def tearDown(self):self.tmp.cleanup()
    def test_cached_and_mime(self):
        self.e.store.execute("UPDATE sessions SET avatar='https://example.invalid/a.jpg' WHERE id='s'")
        jpeg=b'\xff\xd8\xff\xe0'+b'x'*32
        with patch.object(self.e,'fetch_avatar',return_value=jpeg) as fetch:
            data,mime=self.e.avatar('s')
            self.assertEqual((mime,data),('image/jpeg',jpeg))
            data2,mime2=self.e.avatar('s')
            self.assertEqual((mime2,data2),('image/jpeg',jpeg))
            self.assertEqual(fetch.call_count,1)
    def test_missing(self):
        self.assertIsNone(self.e.avatar('s'))
        self.assertIsNone(self.e.avatar('nope'))
    def test_failed_fetch(self):
        self.e.store.execute("UPDATE sessions SET avatar='https://example.invalid/a.jpg' WHERE id='s'")
        with patch.object(self.e,'fetch_avatar',return_value=None):self.assertIsNone(self.e.avatar('s'))

if __name__=='__main__':unittest.main()
