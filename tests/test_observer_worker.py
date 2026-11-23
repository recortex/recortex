"""Deterministic boundary tests; fake extraction/review does not measure semantic quality."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'skills/recortex/scripts'))
import index
import observer
import observer_runtime as runtime
import observer_sources as sources
import observer_store as store
import observer_worker as worker
from tests.observer_source_fixture import fixture_trusted_ancestors

REPO='octo-org/octo-repo'
QUOTE='For the parser, use strict quoting and keep the warning.'
BODY='''<!-- ghim:event type=decision at=2026-03-01T12:00:00Z -->
### Decision: 2026-03-01 12:00 UTC - Strict parser

**In plain words, we decided to:** keep strict quoting with a warning

**Decision:** Use strict quoting and keep the warning.

**Decided by:** Owner, 2026-03-01 12:00 UTC

**Exact words:**

> For the parser, use strict quoting and keep the warning.

**Where recorded:** Original user message in the active session; source identity accompanies this record.

**Effect:** Strict quoting remains the default.

**Next step:** Update the parser tests, owner maintainer
'''
CORRECTION='''<!-- ghim:event type=correction at=2026-03-01T12:00:00Z -->
### Correction: 2026-03-01 12:00 UTC - Quoting default

**In plain words, we corrected ourselves:** strict quoting stays the default of the reader

**Corrects:** the earlier note on the quoting default

**Was:** relaxed quoting is the default

**Is:** strict quoting is the default, as the user said in this session

**Why:** the earlier note misread the session

**Also updated:** Nothing else
'''
# A stretch of a live check at Everything (invented project words): a result the user typed and asked the agent only to
# acknowledge, and the agent's one acknowledging reply. The sorter labeled both messages a result; the drafter of 1.6.0 returned
# no candidate, because the scope rules that opened its prompt read "Just acknowledge it" as a limit. The body is the one that
# the drafter wrote once the rules were limited to decisions.
ACK_USER = ('I measured it again: the new parser reads the 10 MB file in a median of 0.13 s over 5 runs at commit 7be21c0, and '
            'the standard library csv reader takes 0.11 s on the same file. Just acknowledge it.')
ACK_AGENT = ('Noted. At commit 7be21c0 you measured the new parser at a median of 0.13 s over 5 runs on the 10 MB file, and the '
             'standard library csv reader at 0.11 s on the same file. So the new parser was slower than the standard library '
             "on that file.\n\nI've only acknowledged it, as you asked. I haven't recorded it or posted anything.")
ACK_QUOTE = ('the new parser reads the 10 MB file in a median of 0.13 s over 5 runs at commit 7be21c0, and the standard library '
             'csv reader takes 0.11 s on the same file')
ACK_RESULT = '''<!-- ghim:event type=result at=2026-03-01T12:00+00:00 -->
### Result: 2026-03-01 12:00 UTC - New parser 10 MB read time against the standard library

**In plain words, we found that:** the new parser read the 10 MB file in a median of 0.13 s against 0.11 s for the standard library csv reader, as the user measured

**What ran:** The user reported the run; the agent did not run it. The user measured the new parser on the 10 MB file, 5 runs, at commit 7be21c0, and the standard library csv reader on the same file. The command and output folder are not stated.

**Configuration:** New parser at commit 7be21c0, 10 MB file. Python version, machine and settings not stated.

**Numbers:**

| Measure | Value | Denominator | Source path |
|---|---|---|---|
| Median read time, 10 MB, new parser | 0.13 s | 5 runs | Not stated |
| Read time, 10 MB, standard library csv reader | 0.11 s | Not stated | Not stated |

**Exclusions and failures:** Not stated

**Cost:** Not measured

**Not established:** Spread of the runs, the number of runs for the standard library reader, and whether the 0.02 s gap is larger than run-to-run variation are not stated. Other file sizes were not mentioned.

**Next step:** Not stated, owner not stated
'''
REAL_REFRESH=worker.refresh_record
CONTEXT={'targets':[{'issue':2,'title':'Parser','body':'Strict parser requirements.'}], 'memories':[], 'incomplete':False}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        metadata=fixture_trusted_ancestors(self.base);metadata.__enter__();self.addCleanup(metadata.__exit__,None,None,None)
        self.project=str(self.base/'project')
        env=patch.dict(os.environ,{'XDG_STATE_HOME':str(self.base/'state'),'XDG_CACHE_HOME':str(self.base/'cache'),
                                  'XDG_CONFIG_HOME':str(self.base/'config'),'HOME':str(self.base/'home'),
                                  'CLAUDE_CONFIG_DIR':str(self.base/'claude'),'CLAUDE_PROJECT_DIR':self.project},clear=True)
        env.start();self.addCleanup(env.stop)
        self.config={'repo':REPO,'observer':{'enabled':True,'level':'everything','publisher_id':runtime.publisher_id(create=True)}}
        self.folder=Path(sources.project_directory(self.project));self.folder.mkdir(parents=True)
        for d in (self.folder,self.folder.parent,self.folder.parent.parent):d.chmod(0o700)
        self.path=self.folder/'session-1.jsonl';self.path.write_text('');self.path.chmod(0o600)
        self.hook={'session_id':'session-1','transcript_path':str(self.path),'hook_event_name':'Stop'}
        with store.Store(REPO,config=self.config) as db:
            db.set_control('runtime_gate',{'fake_test_only':True})
        runtime.approve(REPO,self.config)
        for target,value in ((worker.recortex,'allowed_users'),):
            p=patch.object(target,value,return_value={'octocat'});p.start();self.addCleanup(p.stop)
        p=patch.object(runtime,'gate_valid',return_value=True);p.start();self.addCleanup(p.stop)
        p=patch.object(worker,'record_context',return_value=CONTEXT);p.start();self.addCleanup(p.stop)
        p=patch.object(worker,'refresh_record',return_value=True);p.start();self.addCleanup(p.stop)
        p=patch.object(worker,'ENOUGH',1);p.start();self.addCleanup(p.stop)  # the wait for a stretch has its own test
        self.calls=[]
        self.texts={}
    def capture(self,text=QUOTE,role='user'):
        with self.path.open('a') as f:f.write(json.dumps({'type':role,'userType':'external','message':{'role':role,'content':text},'timestamp':'2026-03-01T12:00:00Z'})+'\n')
        rows,cursor=sources.scan(self.hook,self.project)
        with store.Store(REPO,config=self.config) as db:db.capture(rows)
        self.texts[rows[-1]['source_id']]=text
        return rows[-1]['source_id']
    def candidate(self,sid,**changes):
        return dict({'kind':'decision','issue':2,'body':BODY,'sources':[sid],'excerpts':[QUOTE]},**changes)
    def sid_of(self,view):
        """The captured source a model was shown, found by its text: the models see numbers, never ids."""
        return next((sid for sid,text in self.texts.items() if text==view['text']),None)
    def shown(self,payload):
        return [self.sid_of(view) for view in payload['sources']]
    def number(self,sid,payload):
        """The number the payload gives a captured source (one past the stretch for a source it does not hold)."""
        found=[view['n'] for view in payload['sources'] if view['text']==self.texts.get(sid)]
        return found[0] if found else len(payload['sources'])+1
    def sorting(self,candidates,payload,label=None):
        """What a sorter that finds exactly these candidates answers: one memory label for each, citing its messages."""
        labels=[]
        for c in candidates:
            if not c:continue
            cited=[self.number(s,payload) for s in c['sources'] if s in self.texts]
            kind=label or (c['kind'] if c['kind'] in worker.LABEL_KINDS else 'none')
            if cited:labels.append({'job':'memory','kind':kind,'sources':cited,'task':0,'steps':[],'parts':[]})
        return {'labels':labels,'ignored':[]}
    def drafted(self,c,payload):
        """A candidate as the drafter returns it: the messages by their numbers."""
        return {'kind':c['kind'],'issue':c['issue'],'task_title':c.get('task_title',''),'topic':c.get('topic',0),'body':c['body'],
                'sources':[self.number(s,payload) for s in c['sources']],'excerpts':c['excerpts'],
                'marks':c.get('marks',[]),'links':c.get('links',[])}
    def invoke(self,*candidates,accepted=True,bad_hash=False,reason=None,label=None,qualification=None):
        def call(payload,schema,purpose,config):
            self.calls.append((purpose,payload))
            if purpose=='sort':return {'ok':True,'structured':self.sorting(candidates,payload,label)}
            if purpose=='draft':return {'ok':True,'structured':{'candidates':[self.drafted(c,payload) for c in candidates if c]}}
            answer = {'candidate_hash':'0'*64 if bad_hash else payload['candidate_hash'],'accepted':accepted,
                      'reason':reason or ('supported' if accepted else 'unsupported')}
            if qualification is not None:
                self.assertIn("qualification", schema["required"])
                answer["qualification"] = qualification
            return {'ok':True,'structured':answer}
        return call
    def test_a_status_change_rests_on_the_users_words_or_the_agents_message(self):
        # The observer takes notes of the conversation: a change of an earlier memory's status rests on the user's
        # typed words or on the agent's message to the user, and on nothing else.
        said = 'No, the earlier note was wrong: strict quoting stays the default.'
        main = {'subagent': False, 'sidechain': False}
        supplied = {'agent': dict(main, role='assistant', text='I reran the parser tests: strict quoting passes 12 of 12.'),
                    'user': {'role': 'user', 'direct_user': True, 'text': QUOTE},
                    'said': {'role': 'user', 'direct_user': True, 'text': said},
                    'short': {'role': 'user', 'direct_user': True, 'text': 'Go ahead, this is fine.'},
                    'wrapper': {'role': 'user', 'direct_user': False, 'text': said},
                    'subagent': {'role': 'assistant', 'subagent': True, 'sidechain': False, 'text': 'Strict quoting passes.'},
                    'sidechain': {'role': 'assistant', 'subagent': False, 'sidechain': True, 'text': 'Strict quoting passes.'},
                    'unmarked': {'role': 'assistant', 'text': 'Strict quoting passes.'},
                    'tool': {'role': 'tool', 'text': 'strict: 12 of 12 files pass'}}
        correction = {'kind': 'correction', 'body': ''}
        quoting = {'kind': 'correction', 'body': CORRECTION.replace('**Why:** the earlier note misread the session',
                                                                    '**Why:** the user said so:\n\n> ' + said)}
        decision = {'kind': 'decision', 'body': BODY}
        short = BODY.replace('> For the parser, use strict quoting and keep the warning.', '> Go ahead, this is fine.')
        # The user's words back a change only when the memory quotes them, at least MIN_WORDS words and half the message.
        cases = [(correction, ['agent'], 'agent'), (quoting, ['said'], 'user'), (correction, ['said'], ''),
                 (quoting, ['user'], ''), (correction, ['short'], ''),
                 (correction, ['wrapper'], ''), (correction, ['subagent'], ''), (correction, ['sidechain'], ''),
                 (correction, ['unmarked'], ''), (correction, ['tool'], ''),
                 (correction, ['agent', 'tool'], ''), (decision, ['user'], 'user'),
                 (dict(decision, body=short), ['short'], ''), (dict(decision, body=short), ['short', 'agent'], 'agent')]
        for candidate, cited, authority in cases:
            self.assertEqual(authority, worker.status_authority(dict(candidate, sources=cited), supplied), cited)
    def test_checks_closing_a_question_is_a_change_of_status(self):
        import observer_publisher
        closing = BODY.replace('at=2026-03-01T12:00:00Z -->', 'at=2026-03-01T12:00:00Z checks=123 -->')
        self.assertTrue(observer_publisher.status_change(closing))
        self.assertEqual('status_change', observer_publisher.outbound_problem(closing, REPO))
        short = closing.replace('> For the parser, use strict quoting and keep the warning.', '> ok')
        supplied = {'user': {'role': 'user', 'direct_user': True, 'text': 'ok'}}
        self.assertEqual('', worker.status_authority({'kind': 'decision', 'body': short, 'sources': ['user']}, supplied))
    def run_worker(self,invoke):return worker.run_once(REPO,self.config,self.project,invoke)
    def test_model_text_cannot_reach_outside_the_record(self):
        cases = {'outbound_markup': '![s](https://collector.example.test/p.png?d=6768)',
                 'outbound_mention': 'cc @hubot', 'outbound_address': 'see https://collector.example.test/x'}
        for n, (reason, extra) in enumerate(sorted(cases.items())):
            self.calls = []
            sid = self.capture(QUOTE + ' Case %d.' % n)
            body = BODY.replace('**Effect:** Strict quoting remains the default.',
                                '**Effect:** Strict quoting remains the default. ' + extra)
            self.run_worker(self.invoke(self.candidate(sid, body=body)))
            self.assertEqual(['sort', 'draft'], [x[0] for x in self.calls], reason)
            with store.Store(REPO, config=self.config) as db:
                self.assertEqual(reason, db.observation(sid)['reason'])
    def status_case(self, kind, body, sids, excerpts):
        self.calls = []
        with patch.object(worker, 'record_context', return_value=dict(CONTEXT, memories=[{'id': 123}])):
            report = self.run_worker(self.invoke(self.candidate(sids[-1], kind=kind, body=body, sources=sids,
                                                               excerpts=excerpts)))
        self.assertEqual(['sort', 'draft', 'review'], [x[0] for x in self.calls], kind)
        with store.Store(REPO, config=self.config) as db:
            return db.event(report['ready'][0])
    def test_a_change_of_status_posts_on_its_own_only_on_what_was_said(self):
        import observer_publisher
        idea = (ROOT / 'examples/octo-repo/chain/1-idea.md').read_text()
        replacing = BODY.replace('at=2026-03-01T12:00:00Z -->', 'at=2026-03-01T12:00:00Z replaces=123 -->')
        said = 'No, the earlier note was wrong: strict quoting stays the default.'
        reported = 'I reran the parser tests after the change: strict quoting passes 12 of 12 files.'
        cases = [('idea', idea.replace('-08:00 -->', '-08:00 replaces=123 -->'), lambda: [self.capture('Reader idea.', role='assistant')], [], 'agent'),
                 # the user's own typed correction, which the memory quotes
                 ('correction', CORRECTION.replace('**Why:** the earlier note misread the session',
                                                   '**Why:** the user said so:\n\n> ' + said),
                  lambda: [self.capture(said)], [said], 'user'),
                 # a long typed message the memory does not quote backs nothing on its own
                 ('correction', CORRECTION, lambda: [self.capture(said + ' Really.')], [], ''),
                 # what the agent told the user it found
                 ('correction', CORRECTION, lambda: [self.capture(reported, role='assistant')], [reported], 'agent'),
                 # a short phrase on its own backs nothing
                 ('correction', CORRECTION, lambda: [self.capture('Go ahead, this is fine.')], [], ''),
                 # a decision whose own Exact words quote the user's whole statement
                 ('decision', replacing, lambda: [self.capture(QUOTE)], [QUOTE], 'user')]
        for kind, body, sids, excerpts, authority in cases:
            event = self.status_case(kind, body, sids(), excerpts)
            self.assertEqual(authority, event['review']['status_authority'], (kind, excerpts))
            problem = observer_publisher.outbound_problem(event['body'], REPO, status_ok=bool(authority))
            self.assertEqual(None if authority else 'status_change', problem, (kind, excerpts))
    def test_a_change_of_status_backed_by_a_short_user_phrase_waits_for_the_main_session(self):
        # The user only said "go ahead": that backs no replacement on its own, and neither does part of a longer
        # message.
        planted = BODY.replace('at=2026-03-01T12:00:00Z -->', 'at=2026-03-01T12:00:00Z replaces=123 -->').replace(
            '> For the parser, use strict quoting and keep the warning.', '> Go ahead, this is fine.')
        event = self.status_case('decision', planted, [self.capture('Go ahead, this is fine.')], ['Go ahead, this is fine.'])
        self.assertEqual('', event['review']['status_authority'])
        long_message = QUOTE + ' ' + 'Also, separately, look at the build scripts and the docs and the release notes later.'
        partial = self.status_case('decision', BODY.replace('at=2026-03-01T12:00:00Z -->', 'at=2026-03-01T12:00:00Z replaces=123 -->'),
                                   [self.capture(long_message)], [QUOTE])
        self.assertEqual('', partial['review']['status_authority'])  # less than half of the user's message
        correction = self.status_case('correction', CORRECTION, [self.capture('Fine, go ahead.')], ['Fine, go ahead.'])
        self.assertEqual('', correction['review']['status_authority'])
    def test_a_memory_the_main_session_posted_is_not_posted_again(self):
        before = self.capture()
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual([], db.register_main(2, 'decision', 9200))
        report = self.run_worker(self.invoke(self.candidate(before)))
        self.assertEqual(['sort', 'draft'], [x[0] for x in self.calls])
        self.assertEqual((1, []), (report['ignored'], report['ready']))
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual(('ignored', 'main_session'), (db.observation(before)['state'], db.observation(before)['reason']))
        self.calls = []
        later = self.capture(QUOTE + ' Later.')  # what happens after the main session's post is the observer's again
        self.assertEqual(1, len(self.run_worker(self.invoke(self.candidate(later)))['ready']))
    def test_a_moment_the_user_withheld_is_not_posted_on_its_own_and_not_offered_to_the_models_again(self):
        # The reviewer has a reason of its own for a moment the user asked not to post or record, and the selector is
        # told to skip such a moment. Since 1.5.1 the candidate is kept for the user's answer (Post or Drop), not lost.
        self.assertIn('withheld',worker.REVIEW['properties']['reason']['enum'])
        self.assertIn('not to post or record',runtime.PROMPTS['sort'])
        self.assertIn('not to post or record',runtime.PROMPTS['draft'])
        self.assertIn('withholds nothing',runtime.PROMPTS['review'])
        self.assertIn('reason withheld',runtime.PROMPTS['review'])
        for purpose in ('sort','draft'):self.assertNotIn('even when the user asked',runtime.PROMPTS[purpose])
        sid=self.capture()
        call=self.invoke(self.candidate(sid),accepted=False,reason='withheld')
        self.assertEqual([],self.run_worker(call)['ready'])
        with store.Store(REPO,config=self.config) as db:
            self.assertEqual(('deferred','withheld_review'),(db.observation(sid)['state'],db.observation(sid)['reason']))
            [event]=db.events()   # kept for the user's answer: deferred and held, never ready
            self.assertEqual(('deferred','withheld_review',1),(event['state'],event['reason'],event['held']))
        self.calls=[];self.run_worker(call);self.assertEqual([],self.calls)
    def test_the_reviewer_sees_the_rest_of_the_stretch_as_context(self):
        # The cited messages are the evidence; what else was said around them in the session is context, so that a
        # command the user named or a commit the agent gave supports the candidate too. Authority rests on the cited
        # messages alone.
        asked = self.capture('Run python3 bench.py at commit 4f2a9c1 and tell me the median.')
        told = self.capture('The median was 1.31 s over 5 runs.', role='assistant')
        sid = self.capture()
        self.run_worker(self.invoke(self.candidate(sid)))
        review = [payload for purpose, payload in self.calls if purpose == 'review'][0]
        self.assertEqual([QUOTE], [item['text'] for item in review['sources']])
        self.assertEqual([self.texts[asked], self.texts[told]], [item['text'] for item in review['context']])
        self.assertEqual([3], review['candidate']['sources'])   # the messages are cited by number, never by id
        self.assertNotRegex(json.dumps(dict(review, candidate_hash='')), '[0-9a-f]{64}')
        self.assertIn('the other messages of the same stretch', runtime.PROMPTS['review'])
        self.assertIn("the agent's account of such a request withholds nothing", runtime.PROMPTS['review'])
    def test_no_field_sheet_mark_or_pattern_reaches_the_models(self):
        # The observer does not judge confidentiality: a marked project's sheet, its "Never write down" line and the
        # project's restricted patterns stay out of both calls; the mechanical refusals still apply to every body.
        self.config['restricted_patterns'] = [{'name': 'site code', 'pattern': '\\bST-[0-9]{3}\\b', 'example': 'ST-204'}]
        sheet = {'state': 'approved', 'entry': {'brief': {'never': 'names of client sites'}}, 'marks': ['people']}
        sid = self.capture()
        import field_sheet
        with patch.object(field_sheet, 'state', return_value=sheet):
            ready = self.run_worker(self.invoke(self.candidate(sid)))['ready']
        self.assertEqual(['sort', 'draft', 'review'], [purpose for purpose, _ in self.calls])
        for purpose, payload in self.calls:
            text = json.dumps(payload)
            for absent in ('never_write_down', 'names of client sites', 'restricted_patterns', 'ST-', 'confidential',
                           'people'):
                self.assertNotIn(absent, text, (purpose, absent))
        self.assertEqual(1, len(ready))
        self.assertNotIn('confidential', json.dumps(worker.REVIEW))
    def test_a_final_message_written_after_stop_is_captured_by_the_worker(self):
        # Claude Code writes a turn's final message after its Stop hook. The worker that hook starts waits for the
        # transcript to settle and captures again before it selects: a message written 200 ms after Stop is read.
        import threading
        import observer_hooks
        with self.path.open('a') as f:
            f.write(json.dumps({'type': 'user', 'userType': 'external', 'message': {'role': 'user', 'content': QUOTE},
                                'timestamp': '2026-03-01T12:00:00Z'}) + '\n')
        with store.Store(REPO, config=self.config) as db:
            observer_hooks._capture(db, 'Stop', self.hook, self.project, REPO, self.config)  # the Stop hook
        final = 'I ran the parser tests: strict quoting passes 12 of 12.'
        def late():
            time.sleep(0.2)
            with self.path.open('a') as f:
                f.write(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'model': 'claude-test',
                                                                     'content': [{'type': 'text', 'text': final}]},
                                    'timestamp': '2026-03-01T12:00:05Z'}) + '\n')
        writer = threading.Thread(target=late)
        writer.start()
        started = time.monotonic()
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual(1, worker.settle(db, REPO, self.config, self.project))
            said = [sources.resolve(row['metadata'], self.project)['text'] for row in db.pending(limit=10)]
        writer.join()
        self.assertIn(final, said)
        self.assertLess(time.monotonic() - started, worker.SETTLE_SECONDS + 1)
        # The worker a hook starts settles first; one run for a retry does not.
        calls = []
        with patch.object(worker, 'settle', side_effect=lambda *args, **kwargs: calls.append(1) or 0):
            worker.run_once(REPO, self.config, self.project, lambda *args: {'ok': False}, settle_first=True)
            worker.run_once(REPO, self.config, self.project, lambda *args: {'ok': False})
        self.assertEqual([1], calls)

    def test_a_session_kept_off_the_record_is_not_selected_from(self):
        # A publish hold on a session: its sources are set aside before any call, and are not offered again; another
        # session's are selected as usual. A withheld event at publication is not a degraded worker.
        import observer_holds
        import observer_publisher
        hold = observer_holds.make_hold({'source_id': 's1', 'role': 'user', 'direct_user': True,
                                         'text': 'off the record'}, REPO, 'publish', scope={'session_id': 'session-1'})
        with store.Store(REPO, config=self.config) as db:
            db.update_holds(lambda holds: holds + [hold])
        withheld = [self.capture(QUOTE + ' Number %d.' % n) for n in range(3)]
        report = self.run_worker(self.invoke(self.candidate(withheld[0])))
        self.assertEqual([], self.calls)
        self.assertEqual(3, report['deferred'])
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual({'withheld'}, {db.observation(sid)['reason'] for sid in withheld})
        self.run_worker(self.invoke(self.candidate(withheld[0])))
        self.assertEqual([], self.calls)  # not offered again
        self.hook = dict(self.hook, session_id='session-2', transcript_path=str(self.folder / 'session-2.jsonl'))
        self.path = self.folder / 'session-2.jsonl'
        self.path.write_text('')
        self.path.chmod(0o600)
        other = self.capture()
        self.config['observer']['mode'] = 'automatic'
        runtime.approve(REPO, self.config)
        with patch.object(observer_publisher, 'publish', side_effect=observer_publisher.PublishError('withheld')):
            ready = self.run_worker(self.invoke(self.candidate(other)))['ready']
        self.assertEqual(['sort', 'draft', 'review'], [purpose for purpose, _ in self.calls])
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual(('deferred', 'withheld'), (db.event(ready[0])['state'], db.event(ready[0])['reason']))
            self.assertFalse(db.get_control('worker_degraded'))

    def test_the_reviewer_sees_the_newest_memories_on_the_issue(self):
        # To reject a repeat (a result told again after a rerun, with nothing new), the reviewer gets the newest
        # memories by allowed accounts on the candidate's issue, checkpoints aside; the selector does not need them.
        items = {n: {"id": n, "issue": 2 if n < 12 else 3, "kind": "checkpoint" if n == 5 else "result",
                     "at": "2026-03-01T12:%02d:00Z" % n, "time": n, "title": "Title %d" % n,
                     "plain": None if n == 7 else "strict quoting passes %d of 12" % n} for n in range(1, 14)}
        class Index:
            def close(self):
                pass
        with patch.object(worker.index, 'connect', return_value=Index()), \
                patch.object(worker.index, 'memories', return_value={'items': items}) as read:
            recent = worker.recent_on_issue(REPO, {2})
        self.assertEqual({'octocat'}, read.call_args.args[1])
        self.assertEqual([11, 10, 9, 8, 7, 6, 4, 3], [m['id'] for m in recent[2]])
        self.assertEqual('Title 7', recent[2][4]['said'])
        self.assertEqual({'id': 11, 'at': '2026-03-01T12:11', 'kind': 'result', 'said': 'strict quoting passes 11 of 12'},
                         recent[2][0])
        sid = self.capture()
        with patch.object(worker, 'recent_on_issue', return_value=recent):
            self.run_worker(self.invoke(self.candidate(sid)))
        payloads = dict(self.calls)
        self.assertEqual(recent[2], payloads['review']['recent_on_issue'])
        self.assertNotIn('recent_on_issue', payloads['sort'])
        self.assertNotIn('recent_on_issue', payloads['draft'])
        self.assertIn('recent_on_issue lists the newest memories', runtime.PROMPTS['review'])
        self.calls = []
        sid = self.capture(QUOTE + ' Once more.')
        with patch.object(worker, 'recent_on_issue', side_effect=sqlite3.OperationalError('locked')):
            self.run_worker(self.invoke(self.candidate(sid)))
        self.assertEqual([], dict(self.calls)['review']['recent_on_issue'])  # judged without it

    def test_a_chat_channels_message_is_not_a_source(self):
        # A channel's message counts for holds only: the worker sets it aside before any call.
        sid = self.capture('<channel source="plugin:telegram:telegram" chat_id="1001" message_id="9" user="owner" '
                           'ts="2026-11-16T10:00:00Z">\nWe decided to keep strict quoting for every import.\n</channel>')
        report = self.run_worker(self.invoke(self.candidate(sid)))
        self.assertEqual([], self.calls)
        self.assertEqual(1, report['ignored'])
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual(('ignored', 'not_typed'), (db.observation(sid)['state'], db.observation(sid)['reason']))

    def test_unbound_candidates_never_reach_the_reviewer(self):
        cases = {'excerpt_binding': dict(excerpts=['words that are not in the source']),
                 'destination': dict(issue=99),
                 'link_binding': dict(body=BODY.replace('at=2026-03-01T12:00:00Z -->', 'at=2026-03-01T12:00:00Z because=777 -->')),
                 'privacy': dict(body=BODY.replace('**Effect:**', '**Effect:** token ghp_' + 'A' * 36 + '.'))}
        for n, (reason, change) in enumerate(sorted(cases.items())):
            self.calls = []
            sid = self.capture(QUOTE + ' Unbound %d.' % n)
            self.run_worker(self.invoke(self.candidate(sid, **change)))
            self.assertEqual(['sort', 'draft'], [x[0] for x in self.calls], reason)
            with store.Store(REPO, config=self.config) as db:
                self.assertEqual(reason, db.observation(sid)['reason'])
    def test_one_failing_event_does_not_stop_the_queue(self):
        import observer_publisher
        self.config['observer']['mode'] = 'automatic'
        runtime.approve(REPO, self.config)
        first, second = self.capture(), self.capture(QUOTE + ' Again.')
        call = self.invoke(self.candidate(first), self.candidate(second))
        with patch.object(observer_publisher, 'publish', side_effect=[observer_publisher.PublishError('lint_failed'),
                                                                     {'state': 'verified'}]) as publish:
            ready = self.run_worker(call)['ready']
        self.assertEqual(2, publish.call_count)
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual(('deferred', 'lint_failed'), (db.event(ready[0])['state'], db.event(ready[0])['reason']))
            self.assertEqual('publish_lint_failed', db.get_control('worker_degraded'))
    def test_daily_cap_stops_the_pass_and_the_rest_waits_in_the_queue(self):
        import observer_publisher
        self.config['observer']['mode'] = 'automatic'
        runtime.approve(REPO, self.config)
        first, second = self.capture(), self.capture(QUOTE + ' Again.')
        call = self.invoke(self.candidate(first), self.candidate(second))
        with patch.object(observer_publisher, 'publish', side_effect=observer_publisher.PublishError('daily_cap')) as publish:
            ready = self.run_worker(call)['ready']
        self.assertEqual(1, publish.call_count)
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual(['ready', 'ready'], [db.event(key)['state'] for key in ready])
            self.assertEqual('publish_daily_cap', db.get_control('worker_degraded'))
    def test_two_independent_passes_bind_review_before_persistence(self):
        sid=self.capture();candidate=self.candidate(sid)
        report=self.run_worker(self.invoke(candidate))
        self.assertEqual('ok',report['code']);self.assertEqual(1,len(report['ready']))
        self.assertEqual(['sort','draft','review'],[x[0] for x in self.calls])
        with store.Store(REPO,config=self.config) as db:
            event=db.event(report['ready'][0]);self.assertTrue(db.review_valid(event));self.assertEqual('ready',event['state'])
            self.assertEqual(3,db.status()['invocations_24h'])
    def test_overlapping_candidates_never_demote_ready_source(self):
        sid=self.capture();candidate=self.candidate(sid)
        report=self.run_worker(self.invoke(candidate,candidate))
        self.assertEqual(1,len(report['ready']))
        with store.Store(REPO,config=self.config) as db:self.assertEqual('ready',db.observation(sid)['state'])
    def test_wrong_review_hash_and_rejection_never_persist_raw_draft(self):
        sid=self.capture();candidate=self.candidate(sid)
        report=self.run_worker(self.invoke(candidate,bad_hash=True));self.assertFalse(report['ready'])
        with store.Store(REPO,config=self.config) as db:
            self.assertEqual([],db.events());self.assertNotIn(BODY.encode(),Path(db.path).read_bytes())
            self.assertEqual('review_failed',db.observation(sid)['reason'])
    def test_one_run_qualifier_omitted_from_quote_cannot_become_a_standing_decision(self):
        sid = self.capture("For this run only: " + QUOTE)
        self.run_worker(self.invoke(self.candidate(sid)))
        self.assertEqual(["sort", "draft"], [call[0] for call in self.calls])
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual([], db.events())

    def test_separate_question_or_trial_goes_to_review_with_the_whole_decision_source(self):
        for prefix, suffix, accepted, qualification, expected in (
                ("What if we switch the logging format? ", "", True, "separate_action", "ready"),
                ("", " That is our permanent parser policy. For this run only, leave the documentation untouched.",
                 True, "separate_action", "ready"),
                ("", " That is our permanent parser policy. For this run only, leave the parser documentation untouched.",
                 True, "separate_action", "ready"),
                ("", " For this run only, keep strict quoting.", True, "same_decision", "review_rejected"),
                ("", " For this run only, do that.", True, "unclear", "review_rejected"),
                ("", " For this run only, enable that setting.", True, None, "review_failed"),
                ("", " For this run only, rotate the logs.", False, "separate_action", "review_rejected")):
            text = prefix + QUOTE + suffix
            with self.subTest(text=text):
                sid = self.capture(text)
                self.calls = []
                self.run_worker(self.invoke(self.candidate(sid), accepted=accepted, qualification=qualification))
                self.assertEqual(["sort", "draft", "review"], [purpose for purpose, _ in self.calls])
                self.assertEqual(text, self.calls[-1][1]["sources"][0]["text"])
                with store.Store(REPO, config=self.config) as db:
                    if expected == "ready":
                        self.assertEqual("ready", db.event_by_source(sid)["state"])
                    else:
                        self.assertIsNone(db.event_by_source(sid))
                        self.assertEqual(expected, db.observation(sid)["reason"])

    def test_assistant_quote_cannot_create_user_decision(self):
        sid=self.capture(role='assistant')
        self.run_worker(self.invoke(self.candidate(sid),label='idea'))   # a sorter that mislabels the moment as an idea
        self.assertEqual(['sort','draft'],[x[0] for x in self.calls])
        with store.Store(REPO,config=self.config) as db:self.assertEqual('decision_authority',db.observation(sid)['reason'])
    def test_model_cannot_bind_unseen_source_or_destination(self):
        sid=self.capture();bad=self.candidate(sid,sources=[sid,'f'*64])
        self.run_worker(self.invoke(bad));self.assertEqual(['sort','draft'],[x[0] for x in self.calls])
        with store.Store(REPO,config=self.config) as db:self.assertEqual([],db.events())
    def test_secret_source_never_reaches_model(self):
        sid=self.capture('token ghp_'+'A'*36)
        self.run_worker(self.invoke(self.candidate(sid)))
        self.assertEqual([],self.calls)
        with store.Store(REPO,config=self.config) as db:self.assertEqual('privacy',db.observation(sid)['reason'])
    def test_budget_counts_failed_sorter_and_queued_data_survives(self):
        sid=self.capture()
        def failed(*args):return {'ok':False,'structured':None}
        self.assertEqual('sort-failed',self.run_worker(failed)['code'])
        with store.Store(REPO,config=self.config) as db:
            self.assertEqual(1,db.status()['invocations_24h']);self.assertEqual('deferred',db.observation(sid)['state'])
    def test_permanent_deferred_sources_do_not_starve_new_work(self):
        for n in range(10):
            sid=self.capture('Unavailable '+str(n))
            with store.Store(REPO,config=self.config) as db:db.defer(sid,'source_unavailable')
        sid=self.capture()
        self.assertEqual(1,len(self.run_worker(self.invoke(self.candidate(sid)))['ready']))
    def offered(self,ignore=()):
        """A sorter that labels nothing and sets aside only `ignore`; returns the ids each pass was shown."""
        shown=[]
        def call(payload,schema,purpose,config):
            shown.append(self.shown(payload))
            return {'ok':True,'structured':{'labels':[],'ignored':[v['n'] for v,i in zip(payload['sources'],shown[-1]) if i in ignore]}}
        return shown,call
    def state(self,sid):
        with store.Store(REPO,config=self.config) as db:
            item=db.observation(sid);return item['state'],item['reason']
    def test_uncertain_sources_are_offered_again_as_their_session_goes_on_a_bounded_number_of_times(self):
        # What the selector neither used nor set aside waits for its session to go on (or five minutes), is offered
        # again with what followed, at most three times in all, and then stays deferred, visible in pending, for
        # observer.py record --source; it is never left "uncertain" for good.
        first=self.capture('Run the tests and tell me which one fails.')
        shown,call=self.offered()
        self.run_worker(call)
        self.assertEqual([[first]],shown);self.assertEqual(('deferred','uncertain'),self.state(first))
        self.run_worker(call)  # nothing new in its session yet: not offered again
        self.assertEqual(1,len(shown))
        second=self.capture('The fix is in; run them again.')
        self.run_worker(call)
        self.assertEqual([first,second],shown[-1]);self.assertEqual(('deferred','uncertain_2'),self.state(first))
        with store.Store(REPO,config=self.config) as db:  # five minutes later, with nothing new
            db.db.execute("UPDATE observations SET updated=updated-301")
        self.run_worker(call)
        self.assertEqual([first,second],shown[-1])
        self.assertEqual((('deferred','uncertain_3'),('deferred','uncertain_2')),(self.state(first),self.state(second)))
        third=self.capture('Thanks.')
        self.run_worker(call)
        self.assertEqual([second,third],shown[-1])  # the first had its three attempts
        self.assertEqual(('deferred','uncertain_3'),self.state(first))
        self.assertEqual(('uncertain',1),worker._attempts('uncertain'))
        self.assertEqual(('input_limit',1),worker._attempts('input_limit'))
        self.assertEqual(('candidate_limit',4),worker._attempts('candidate_limit_4'))
        self.assertEqual('candidate_lint_3',worker._again('candidate_lint','uncertain_2'))
    def test_sources_that_did_not_fit_go_first_next_time_and_in_transcript_order(self):
        # One scan captures them all at the same moment, so only the transcript gives their order.
        with self.path.open('a') as f:
            for n in range(6):
                f.write(json.dumps({'type':'user','userType':'external','message':{'role':'user','content':'Note %d. '%n+'x'*5000}})+'\n')
        rows,_=sources.scan(self.hook,self.project)
        with store.Store(REPO,config=self.config) as db:db.capture(rows)
        sids=[row['source_id'] for row in rows]
        self.texts.update({sid:'Note %d. '%n+'x'*5000 for n,sid in enumerate(sids)})
        self.assertNotEqual(sids,sorted(sids))
        shown,call=self.offered(ignore=sids)
        self.run_worker(call)
        fitted=shown[0]
        self.assertEqual(sids[:len(fitted)],fitted)  # in the order they happened
        self.assertLess(len(fitted),6)
        left=[sid for sid in sids if sid not in fitted]
        self.assertEqual({('deferred','input_limit')},{self.state(sid) for sid in left})
        later=self.capture('A later note.')
        self.run_worker(call)
        self.assertEqual(left,shown[1][:len(left)])  # the deferred ones first, still in order
        self.assertEqual(('ignored',''),self.state(left[0]))
    def test_one_agent_message_that_tells_two_moments_backs_both(self):
        # The agent's summary told the user of a result and of the idea it raised; the second candidate drops the
        # message the first event already holds and keeps its own source.
        import observer_publisher
        idea = (ROOT / 'examples/octo-repo/chain/1-idea.md').read_text()
        said = self.capture('Idea for later: try one regular expression for the whole text.')
        told = self.capture('Saved two things: the decision, and the regular-expression idea for later.', role='assistant')
        decided = self.capture()
        first = self.candidate(decided, sources=[decided, told], excerpts=[QUOTE])
        second = {'kind': 'idea', 'issue': 2, 'body': idea, 'sources': [said, told], 'excerpts': []}
        report = self.run_worker(self.invoke(first, second))
        self.assertEqual(2, len(report['ready']))
        with store.Store(REPO, config=self.config) as db:
            events = [db.event(key) for key in report['ready']]
        self.assertEqual([sorted([decided, told]), [said]], [sorted(e['candidate']['sources']) for e in events])
    def test_replay_a_typed_result_that_the_user_asked_only_to_acknowledge_is_drafted_reviewed_and_ready(self):
        said = self.capture(ACK_USER)
        told = self.capture(ACK_AGENT, role='assistant')
        result = {'kind': 'result', 'issue': 2, 'body': ACK_RESULT, 'sources': [said, told], 'excerpts': [ACK_QUOTE]}
        report = self.run_worker(self.invoke(result))
        self.assertEqual(1, len(report['ready']), report)
        drafting = [payload for purpose, payload in self.calls if purpose == 'draft'][0]
        # the drafter is shown both messages whole, with the user's asking to acknowledge and the agent's reply to it
        self.assertEqual([ACK_USER, ACK_AGENT], [view['text'] for view in drafting['sources']])
        with store.Store(REPO, config=self.config) as db:
            [event] = [db.event(key) for key in report['ready']]
            self.assertEqual(sorted([said, told]), sorted(event['candidate']['sources']))
            self.assertNotEqual('deferred', db.observation(said)['state'])
            self.assertNotEqual('deferred', db.observation(told)['state'])
    def test_the_draft_prompt_does_not_open_with_scope_rules_and_keeps_them_to_decisions(self):
        # Replayed with the live record, the 1.6.0 prompt (the scope rules first, for every kind) returned no candidate for
        # the stretch above on every try, and the 1.5.1 prompt and this one returned a result every time. The rules for a
        # decision stay, after the role and the kinds, and say what they do not apply to.
        prompt = runtime.PROMPTS['draft']
        self.assertTrue(prompt.startswith("You are Recortex's independent note-taker"))
        self.assertNotIn('Read the full user message for scope. Exploration', prompt)
        scope = prompt.index('Read the full user message for scope when you write a decision or the correction of one')
        self.assertGreater(scope, prompt.index('A source with a hold_released field'))
        self.assertIn('These limits are about decisions only.', prompt)
        self.assertIn('A result or a failure is a memory whatever the user asked the agent to do next', prompt)
        self.assertIn('or to take no action, which is not a request to keep it off the record', prompt)
        self.assertIn('Return only the requested structured result.', prompt[scope:])
    def test_a_candidate_for_an_issue_that_is_not_a_target_is_tried_once_more(self):
        sid=self.capture()
        self.run_worker(self.invoke(self.candidate(sid,issue=99)))
        self.assertEqual(('deferred','destination'),self.state(sid))
        self.capture(QUOTE+' Next.')  # its session goes on
        self.calls=[];self.run_worker(self.invoke(self.candidate(sid,issue=99)))
        self.assertEqual(('deferred','destination_2'),self.state(sid))
        self.calls=[];self.capture(QUOTE+' And on.');self.run_worker(self.invoke(self.candidate(sid,issue=99)))
        self.assertNotIn(sid,[s_ for purpose,payload in self.calls for s_ in self.shown(payload)])
    def test_a_long_source_is_shown_by_its_start_and_its_end(self):
        text='START '+'y'*20000+' END'
        sid=self.capture(text)
        shown=[]
        def call(payload,schema,purpose,config):
            shown.append(payload['sources'][0]['text'])
            return {'ok':True,'structured':{'labels':[],'ignored':[1]}}
        self.run_worker(call)
        self.assertTrue(shown[0].startswith('START '));self.assertTrue(shown[0].endswith(' END'))
        self.assertLessEqual(len(shown[0]),worker.SOURCE_TEXT+100);self.assertIn('characters left out',shown[0])
    def age(self, seconds, column='updated'):
        with store.Store(REPO,config=self.config) as db:
            db.db.execute("UPDATE observations SET %s=%s-?" % (column, column), (seconds,))
    def test_a_source_the_model_cannot_answer_for_is_found_in_halves_and_stops(self):
        # One source that makes every selection call fail once held the whole queue for good: a failed batch is now
        # offered again in halves, a source failing alone is counted, and it stops after three attempts.
        poison=self.capture('POISON message the model cannot answer for.',role='assistant')
        calls=[]
        def call(payload,schema,purpose,config):
            ids=self.shown(payload);calls.append(ids)
            if poison in ids:return {'ok':False,'code':'invalid-output','structured':None}
            return {'ok':True,'structured':{'labels':[],'ignored':[v['n'] for v in payload['sources']]}}
        ordinary=[]
        for n in range(24):
            ordinary+=[self.capture('Ordinary note %d one.'%n,role='assistant'),self.capture('Ordinary note %d two.'%n,role='assistant')]
            self.run_worker(call);self.age(301)
        for _ in range(4):
            self.run_worker(call);self.age(301)
        with store.Store(REPO,config=self.config) as db:
            states=[db.observation(sid)['state'] for sid in ordinary]
            self.assertEqual(('deferred','sort_failed_3'),(db.observation(poison)['state'],db.observation(poison)['reason']))
        self.assertGreaterEqual(states.count('ignored'),40)
        self.assertLessEqual(len(calls),40)
        self.assertEqual(2,sum(ids==[poison] for ids in calls))  # three attempts: in its batch, then twice alone
    def test_a_pass_waits_for_a_stretch_of_the_session(self):
        # A pass at every turn edge spent the day's 60 calls in 30 turns at two sources a call: a pass now waits for
        # ENOUGH sources, or for one that has waited PATIENCE seconds.
        calls=[]
        def call(payload,schema,purpose,config):
            calls.append(len(payload['sources']))
            return {'ok':True,'structured':{'labels':[],'ignored':[v['n'] for v in payload['sources']]}}
        with patch.object(worker,'ENOUGH',8):
            for turn in range(40):
                self.capture('Turn %d: please run the tests.'%turn)
                self.capture('Turn %d: all tests pass.'%turn,role='assistant')
                self.run_worker(call)
            self.assertEqual(10,len(calls));self.assertGreaterEqual(min(calls),8)
            lone=self.capture('One more question.')
            self.run_worker(call);self.assertEqual(10,len(calls))
            self.age(worker.PATIENCE+1,'created')
            self.run_worker(call);self.assertEqual([1],calls[10:])
    def test_ready_events_do_not_starve_new_work(self):
        sid=self.capture();self.run_worker(self.invoke(self.candidate(sid)))
        for n in range(10):
            sid=self.capture(QUOTE+' Additional '+str(n))
            self.assertEqual(1,len(self.run_worker(self.invoke(self.candidate(sid)))['ready']))
    def test_rejected_candidate_not_retried_on_every_hook(self):
        sid=self.capture();self.run_worker(self.invoke(self.candidate(sid),accepted=False))
        self.calls=[];self.run_worker(self.invoke(self.candidate(sid)));self.assertEqual([],self.calls)
    def test_automatic_publishes_existing_ready_when_capture_paused(self):
        sid=self.capture();report=self.run_worker(self.invoke(self.candidate(sid)))
        self.config['observer']['mode']='automatic'
        with store.Store(REPO,config=self.config) as db:
            db.set_control('capture_paused',True)
        runtime.approve(REPO,self.config)
        with patch('observer_publisher.publish') as publish:
            self.run_worker(self.invoke(self.candidate(sid)))
            publish.assert_called_once_with(REPO,report['ready'][0],self.config)
    def test_refresh_interval_and_publication_request(self):
        options=runtime.settings(self.config)
        with store.Store(REPO,config=self.config) as db,patch.object(index,'sync',return_value={'complete':True}) as sync:
            self.assertTrue(REAL_REFRESH(REPO,self.config,db,options));self.assertEqual(1,sync.call_count)
            self.assertTrue(REAL_REFRESH(REPO,self.config,db,options));self.assertEqual(1,sync.call_count)
            db.set_control('sync_needed',True)
            self.assertTrue(REAL_REFRESH(REPO,self.config,db,options));self.assertEqual(2,sync.call_count)
            self.assertFalse(db.get_control('sync_needed'))
            db.set_control('sync_last_attempt',time.time()-301)
            self.assertTrue(REAL_REFRESH(REPO,self.config,db,options));self.assertEqual(3,sync.call_count)
            self.assertEqual(10,sync.call_args.kwargs['deadline'])
    def test_failed_refresh_prevents_inference_and_publication(self):
        sid=self.capture()
        with patch.object(worker,'refresh_record',return_value=False),patch('observer_publisher.publish') as publish:
            report=self.run_worker(self.invoke(self.candidate(sid)))
            self.assertEqual('record-unavailable',report['code']);self.assertEqual([],self.calls);publish.assert_not_called()
    def test_retention_runs_with_runtime_unavailable_and_capture_paused(self):
        with store.Store(REPO,config=self.config) as db:db.set_control('capture_paused',True)
        with patch.object(runtime,'gate_valid',return_value=False),patch.object(store.Store,'expire_handled',autospec=True) as expire:
            self.assertEqual('runtime-gate',self.run_worker(self.invoke({}))['code'])
            self.assertEqual(1,expire.call_count);self.assertEqual(7,expire.call_args.args[1])
            self.run_worker(self.invoke({}));self.assertEqual(1,expire.call_count)
    def test_failure_of_refresh_is_visible_and_rate_limited(self):
        options=runtime.settings(self.config)
        with store.Store(REPO,config=self.config) as db,patch.object(index,'sync',side_effect=worker.recortex.GhError('offline')) as sync:
            self.assertFalse(REAL_REFRESH(REPO,self.config,db,options));self.assertFalse(db.get_control('sync_ok'))
            self.assertFalse(REAL_REFRESH(REPO,self.config,db,options));self.assertEqual(1,sync.call_count)
    def test_a_failed_refresh_is_tried_again_soon_and_a_busy_sync_is_waited_for(self):
        # At session start the brief hook's own sync holds the index: the worker waits for it, and a refresh that
        # still failed is tried again after worker.SYNC_RETRY seconds rather than after the full interval.
        options=runtime.settings(self.config)
        with store.Store(REPO,config=self.config) as db,patch.object(worker.time,'sleep') as sleep, \
                patch.object(index,'sync',side_effect=[index.SyncBusy('busy'),{'complete':True}]) as sync:
            self.assertTrue(REAL_REFRESH(REPO,self.config,db,options))
            self.assertEqual((2,1),(sync.call_count,sleep.call_count))
        with store.Store(REPO,config=self.config) as db,patch.object(worker.time,'sleep'), \
                patch.object(index,'sync',side_effect=index.SyncBusy('busy')) as sync:
            db.set_control('sync_last_attempt',0)
            self.assertFalse(REAL_REFRESH(REPO,self.config,db,options))
            self.assertEqual(len(worker.BUSY_WAITS)+1,sync.call_count)
        self.assertLess(worker.SYNC_RETRY,options['sync_interval_seconds'])
        with store.Store(REPO,config=self.config) as db,patch.object(index,'sync',return_value={'complete':True}) as sync:
            db.set_control('sync_last_attempt',time.time()-worker.SYNC_RETRY+5)
            self.assertFalse(REAL_REFRESH(REPO,self.config,db,options));self.assertEqual(0,sync.call_count)
            db.set_control('sync_last_attempt',time.time()-worker.SYNC_RETRY-1)
            self.assertTrue(REAL_REFRESH(REPO,self.config,db,options));self.assertEqual(1,sync.call_count)
            db.set_control('sync_last_attempt',time.time()-worker.SYNC_RETRY-1)
            self.assertTrue(REAL_REFRESH(REPO,self.config,db,options));self.assertEqual(1,sync.call_count)
    def test_original_restricted_strings_checked_before_json_escaping(self):
        self.config['restricted_patterns']=[{'pattern':r'^PRIVATE-SAMPLE$','name':'private'}]
        sid=self.capture('PRIVATE-SAMPLE')
        self.run_worker(self.invoke(self.candidate(sid)));self.assertEqual([],self.calls)
        self.assertTrue(worker.source_privacy_problem({'tool_results':[{'input':{'command':'PRIVATE\tSAMPLE'}}]},
                        {'restricted_patterns':[{'pattern':r'PRIVATE\s+SAMPLE','name':'private'}]}))
    def test_secret_tool_argument_never_reaches_either_model(self):
        invocation={'type':'assistant','message':{'role':'assistant','content':[{'type':'tool_use','id':'call1','name':'Bash','input':{'command':'echo ghp_'+'A'*36}}]}}
        result={'type':'user','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'call1','content':'Safe output','is_error':False}]}}
        self.path.write_text(json.dumps(invocation)+'\n'+json.dumps(result)+'\n')
        rows,_=sources.scan(self.hook,self.project)
        self.assertEqual([],rows)  # a tool call and its output are not the conversation: never captured
        with store.Store(REPO,config=self.config) as db:db.capture(rows)
        self.run_worker(self.invoke({}));self.assertEqual([],self.calls)
        with store.Store(REPO,config=self.config) as db:self.assertEqual([],db.pending())
    def test_cli_dryrun_and_pause_are_independent(self):
        with patch.object(observer.recortex,'load_config',return_value=self.config),contextlib.redirect_stdout(io.StringIO()):
            with patch.object(worker,'run_once') as run:
                self.assertEqual(0,observer.main(['run-worker','--dry-run']));run.assert_not_called()
            self.assertEqual(0,observer.main(['pause','publication']))
        with store.Store(REPO,config=self.config) as db:
            self.assertTrue(db.get_control('publication_paused'));self.assertFalse(db.get_control('capture_paused',False))
    def test_cli_failed_runtime_check_does_not_certify(self):
        output=io.StringIO()
        proof={'ok':False,'code':'timeout','requested_model':'opus','actual_model':'test-runtime-model'}
        with patch.object(observer.recortex,'load_config',return_value=self.config),contextlib.redirect_stdout(output),patch.object(runtime,'check_runtime',return_value=proof),patch.object(runtime,'gate_valid',return_value=False):
            self.assertEqual(1,observer.main(['runtime-check']))
        reported=json.loads(output.getvalue())
        self.assertEqual('opus',reported['requested_model'])
        self.assertEqual('test-runtime-model',reported['actual_model'])
        with store.Store(REPO,config=self.config) as db:
            self.assertIsNone(db.get_control('runtime_gate'));self.assertEqual(1,db.status()['invocations_24h'])
            self.assertEqual(reported,db.get_control('runtime_last'))


class RecordContextTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.db.executescript(index.SCHEMA)
        self.db.executemany('INSERT INTO meta VALUES (?,?)',[('mind_derived','1'),('sync_complete','1'),('synced_at','2026-03-01T12:00:00Z')])
    def issue(self,n,author='octocat',title='Parser',body='Strict parser handling',edits=1):
        self.db.execute("INSERT INTO issues(number,title,body,state,author,is_pr,timeline_read,edits_read) VALUES(?,?,?,'open',?,0,1,?)",(n,title,body,author,edits))
        self.db.execute("INSERT INTO labels VALUES(?,'final-record')",(n,))
    def context(self,query='parser'):
        with patch.object(index,'connect',return_value=self.db),patch.object(worker.recortex,'allowed_users',return_value={'octocat'}):return worker.record_context(REPO,{},query)
    def test_destination_authors_edits_and_relevance(self):
        self.issue(1,author='outsider');self.issue(2,edits=0);self.issue(3);self.issue(4,title='Colors',body='Blue card')
        self.issue(5);self.db.execute("INSERT INTO timeline(issue,event,actor) VALUES(5,'edited','outsider')")
        result=self.context();self.assertEqual([3],[row['issue'] for row in result['targets']])
    def test_large_destination_is_not_cut_mid_constraint(self):
        self.issue(1,body='Parser must not '+('x'*7000));self.issue(2)
        result=self.context();self.assertEqual([2],[row['issue'] for row in result['targets']]);self.assertEqual(1,result['omitted_targets'])


class ObserverCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.project=self.base/'project';self.project.mkdir(mode=0o700)
        self.config=self.project/'.recortex.json';self.config.write_text(json.dumps({'repo':REPO}));self.config.chmod(0o600)
        trusted=self.base/'trusted';trusted.write_text(REPO+' octocat\n');trusted.chmod(0o600)
        self.env=dict(os.environ,CLAUDE_PROJECT_DIR=str(self.project),RECORTEX_TRUSTED_FILE=str(trusted),
                      XDG_STATE_HOME=str(self.base/'state'),XDG_CACHE_HOME=str(self.base/'cache'),XDG_CONFIG_HOME=str(self.base/'config'))
        self.env.pop('RECORTEX_CONFIG',None)
    def cli(self,*args,answered=True):
        if answered and '--yes' in args and 'enable' in args:
            # the user's recorded answer a session's enable goes on; who may enable is tested in test_jobs_enable
            import observer_grants
            with patch.dict(os.environ,{'XDG_STATE_HOME':str(self.base/'state')}):
                for kind,option in (('level','--level'),('strong','--strong-model')):
                    if option in args:observer_grants.put(os.path.realpath(self.project),kind,args[args.index(option)+1],'toolu_test%d'%time.time_ns())
        result=subprocess.run([sys.executable,str(ROOT/'skills/recortex/scripts/observer.py')]+list(args),
                              cwd=self.project,env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual('',result.stderr)
        return result,json.loads(result.stdout)
    def test_readonly_dryrun_and_status_do_not_create_state(self):
        for args in (('run-worker','--dry-run'),('status',),('pending',)):
            result,value=self.cli(*args);self.assertEqual(0,result.returncode);self.assertEqual('empty',value['code'])
            self.assertFalse((self.base/'state').exists())
    def approvals(self):
        path=self.base/'state'/'recortex'/'private'/'observer-approvals.json'
        return json.loads(path.read_text())['folders'] if path.exists() else {}
    def test_enable_requires_a_model_and_names_the_usage_limits(self):
        before=self.config.read_bytes()
        result,value=self.cli('enable')
        self.assertEqual(1,result.returncode);self.assertEqual('model_required',value['reason'])
        self.assertFalse((self.base/'state').exists())
        result,value=self.cli('enable','--model','sonnet')   # the level is the user's choice: nothing is previewed without one
        self.assertEqual(1,result.returncode);self.assertEqual('level_required',value['reason'])
        result,preview=self.cli('enable','--model','sonnet','--level','everything')
        self.assertEqual(0,result.returncode);self.assertFalse(preview['approved'])
        self.assertEqual('sonnet',preview['settings']['model']);self.assertIn('usage limits',preview['usage'])
        self.assertNotIn('$',json.dumps(preview));self.assertNotIn('billing',preview)
        self.assertFalse((self.base/'state').exists())
        result,approved=self.cli('enable','--model','sonnet','--level','everything','--yes')
        self.assertEqual(0,result.returncode);self.assertTrue(approved['approved'])
        self.assertEqual(before,self.config.read_bytes())  # the shared project file is never written
        self.assertEqual([os.path.realpath(self.project)],list(self.approvals()))
        _,status=self.cli('status');self.assertTrue(status['approved']);self.assertFalse(status['runtime_ready'])
        self.assertEqual('sonnet',status['settings']['model'])
    def test_settings_belong_to_this_machine_and_folder_and_the_project_file_only_switches_off(self):
        self.cli('enable','--model','sonnet','--level','everything','--yes')
        other=self.base/'other';other.mkdir(mode=0o700)
        shared=json.loads(self.config.read_text());shared['observer']={'enabled':True,'mode':'automatic','model':'sonnet'}
        (other/'.recortex.json').write_text(json.dumps(shared));(other/'.recortex.json').chmod(0o600)
        moved=dict(self.env,CLAUDE_PROJECT_DIR=str(other))
        result=subprocess.run([sys.executable,str(ROOT/'skills/recortex/scripts/observer.py'),'status'],cwd=other,
                              env=moved,capture_output=True,text=True,timeout=10)
        status=json.loads(result.stdout);self.assertFalse(status['approved']);self.assertFalse(status['settings']['enabled'])
        self.config.write_text(json.dumps({'repo':REPO,'observer':False}))
        _,status=self.cli('status');self.assertFalse(status['approved']);self.assertEqual('off',status['project_switch'])
        result,value=self.cli('enable','--model','sonnet','--level','everything','--yes')
        self.assertEqual(1,result.returncode);self.assertEqual('project-off',value['code'])
        self.config.write_text(json.dumps({'repo':REPO}))
        _,status=self.cli('status');self.assertTrue(status['approved'])
    def test_disable_removes_only_this_folder(self):
        self.cli('enable','--model','sonnet','--level','everything','--yes')
        other=self.base/'other';other.mkdir(mode=0o700)
        (other/'.recortex.json').write_text(self.config.read_text());(other/'.recortex.json').chmod(0o600)
        moved=dict(self.env,CLAUDE_PROJECT_DIR=str(other))
        run=lambda *args:json.loads(subprocess.run([sys.executable,str(ROOT/'skills/recortex/scripts/observer.py')]+list(args),
                                    cwd=other,env=moved,capture_output=True,text=True,timeout=10).stdout)
        import observer_grants
        with patch.dict(os.environ,{'XDG_STATE_HOME':str(self.base/'state')}):
            observer_grants.put(os.path.realpath(other),'level','everything','toolu_test%d'%time.time_ns())   # the user's answer for that folder
        self.assertTrue(run('enable','--model','sonnet','--level','everything','--yes')['approved'])
        result,value=self.cli('disable');self.assertEqual((1,'disable_unanswered'),(result.returncode,value['code']))   # a pipe: no answer
        _,status=self.cli('status');self.assertTrue(status['approved'])
        import acts
        with patch.dict(os.environ,dict(self.env,XDG_STATE_HOME=str(self.base/'state')),clear=True):
            values=acts.ACTS['disable'].values(acts.Context(str(self.project)))
            observer_grants.put(os.path.realpath(self.project),'disable',acts.ACTS['disable'].value(values),'toolu_disable1')
        result,value=self.cli('disable');self.assertEqual(0,result.returncode);self.assertTrue(value['was_enabled'])
        _,status=self.cli('status');self.assertFalse(status['approved'])
        self.assertTrue(run('status')['approved'])
        self.assertEqual([os.path.realpath(other)],list(self.approvals()))
    def test_billing_variables_are_named_and_never_their_values(self):
        self.env['ANTHROPIC_API_KEY']='sk-ant-example-value'
        _,preview=self.cli('enable','--model','sonnet','--level','everything')
        self.assertIn('ANTHROPIC_API_KEY',preview['billing']);self.assertIn('billed',preview['billing'])
        self.cli('enable','--model','sonnet','--level','everything','--yes')
        result,status=self.cli('status')
        self.assertIn('ANTHROPIC_API_KEY',status['billing']);self.assertNotIn('sk-ant-example-value',result.stdout)
    def test_invalid_model_choice_is_rejected_before_any_write(self):
        before=self.config.read_bytes()
        result,value=self.cli('enable','--model','model with spaces','--level','everything','--yes',answered=False)
        self.assertEqual(1,result.returncode);self.assertEqual('observer-unavailable',value['code'])
        self.assertEqual('observer_model',value['reason'])
        self.assertEqual(before,self.config.read_bytes());self.assertFalse((self.base/'state').exists())
    def on_terminal(self,args,word):
        """observer.py with a pseudo-terminal of its own, answering `word` when it asks."""
        import pty
        pid,fd=pty.fork()
        if pid==0:
            try:
                os.chdir(self.project)
                os.execve(sys.executable,[sys.executable,str(ROOT/'skills/recortex/scripts/observer.py')]+list(args),self.env)
            finally:
                os._exit(127)
        seen=b''
        try:
            while b'to confirm' not in seen:
                seen+=os.read(fd,4096)
            os.write(fd,word+b'\n')
            while True:
                try:
                    chunk=os.read(fd,4096)
                except OSError:
                    break
                if not chunk:break
                seen+=chunk
        finally:
            os.close(fd)
        return os.waitpid(pid,0)[1],seen
    @unittest.skipIf(observer.inside_claude(),"the user's own terminal is outside every Claude Code session")
    def test_publication_pause_survives_process_restart(self):
        self.cli('enable','--model','sonnet','--level','everything','--yes');self.cli('pause','publication')
        _,status=self.cli('status');self.assertTrue(status['publication_paused']);self.assertFalse(status['capture_paused'])
        result,value=self.cli('resume','publication')  # from a pipe, as Claude Code's Bash tool runs it: it needs the user's answer
        self.assertEqual((1,'resume_unanswered'),(result.returncode,value['code']))
        _,status=self.cli('status');self.assertTrue(status['publication_paused'])
        code,seen=self.on_terminal(('resume','publication'),b'resume')
        self.assertEqual(0,code,seen);_,status=self.cli('status');self.assertFalse(status['publication_paused'])
    def test_a_terminal_a_session_made_confirms_nothing_whatever_it_types(self):
        # What a pseudo-terminal helper gives a command: a controlling terminal, and stdin and stdout on it. The session writes
        # the word itself, so inside a session (CLAUDECODE, or a claude program among the parents) the word is never asked.
        import pty
        hold_id=self.hold();self.cli('enable','--model','sonnet','--level','everything','--yes')
        for args,word in ((('release',hold_id),b'release'),(('disable',),b'disable')):
            pid,fd=pty.fork()
            if pid==0:
                try:
                    os.chdir(self.project)
                    os.execve(sys.executable,[sys.executable,str(ROOT/'skills/recortex/scripts/observer.py')]+list(args),
                              dict(self.env,CLAUDECODE='1'))
                finally:
                    os._exit(127)
            seen=b''
            try:
                while True:
                    try:
                        chunk=os.read(fd,4096)
                    except OSError:
                        break
                    if not chunk:break
                    seen+=chunk
                    if b'to confirm' in seen:os.write(fd,word+b'\n')
            finally:
                os.close(fd)
            os.waitpid(pid,0)
            self.assertNotIn(b'to confirm',seen,args);self.assertIn(args[0].encode()+b'_unanswered',seen,args)
        self.assertEqual(['active'],self.held());self.assertEqual([os.path.realpath(self.project)],list(self.approvals()))
    def test_automatic_enable_refused_without_runtime_proof(self):
        result,value=self.cli('enable','--mode','automatic','--model','sonnet','--level','everything','--yes')
        self.assertEqual(1,result.returncode);self.assertFalse(value['approved']);self.assertEqual('runtime-gate',value['code'])
        self.assertEqual({},self.approvals());self.assertFalse((self.base/'state'/'recortex'/'private'/'octo-org__octo-repo').exists())
    def hold(self):
        import observer_holds
        with patch.dict(os.environ,self.env,clear=True):
            with store.Store(REPO,config={}) as db:
                source={'source_id':'s1','role':'user','direct_user':True,'text':'/recortex hold git-push'}
                hold=observer_holds.make_hold(source,REPO,'git-push')
                db.update_holds(lambda holds:holds+[hold])
        return hold['id']
    def held(self):
        with patch.dict(os.environ,self.env,clear=True):
            with store.Store(REPO,config={}) as db:return [hold['state'] for hold in db.holds()]
    def test_release_refuses_without_a_terminal_of_the_users_own(self):
        hold_id=self.hold()
        command=[sys.executable,str(ROOT/'skills/recortex/scripts/observer.py'),'release',hold_id]
        # Pipes and no controlling terminal: what Claude Code's Bash tool gives a command.
        done=subprocess.run(command,cwd=self.project,env=self.env,input='release\n',capture_output=True,text=True,
                            timeout=10,start_new_session=True)
        self.assertEqual(1,done.returncode);self.assertEqual('release_unanswered',json.loads(done.stdout)['code'])
        # A terminal on stdin and stdout, but no controlling terminal: /dev/tty cannot be opened.
        import pty
        main,child=pty.openpty()
        try:
            done=subprocess.run(command,cwd=self.project,env=self.env,stdin=child,stdout=child,stderr=subprocess.PIPE,
                                timeout=10,start_new_session=True)
            os.close(child);child=None
            self.assertEqual(1,done.returncode)
            self.assertIn(b'release_unanswered',os.read(main,4096))
        finally:
            os.close(main)
            if child is not None:os.close(child)
        self.assertEqual(['active'],self.held())
    def test_release_refuses_pipes_even_when_a_controlling_terminal_exists(self):
        # Claude Code runs in the user's terminal, so a command it starts may have that controlling terminal while
        # its stdin and stdout are pipes: /dev/tty would then reach the user's real terminal.
        import pty, select
        hold_id=self.hold()
        pid,fd=pty.fork()
        if pid==0:
            try:
                os.chdir(self.project)
                read,_=os.pipe();os.dup2(read,0);os.dup2(os.open(os.devnull,os.O_WRONLY),1)
                os.execve(sys.executable,[sys.executable,str(ROOT/'skills/recortex/scripts/observer.py'),'release',hold_id],self.env)
            finally:
                os._exit(127)
        seen=b''
        try:
            while select.select([fd],[],[],5)[0]:
                try:
                    chunk=os.read(fd,4096)
                except OSError:
                    break
                if not chunk:break
                seen+=chunk
                if b'Type release to confirm' in seen:os.write(fd,b'release\n')
        finally:
            os.close(fd)
        os.waitpid(pid,0)
        self.assertNotIn(b'Type release to confirm',seen);self.assertEqual(['active'],self.held())
    @unittest.skipIf(observer.inside_claude(),"the user's own terminal is outside every Claude Code session")
    def test_release_succeeds_in_a_terminal_after_the_user_confirms(self):
        import pty
        hold_id=self.hold()
        def run(answer):
            pid,fd=pty.fork()
            if pid==0:
                try:
                    os.chdir(self.project)
                    os.execve(sys.executable,[sys.executable,str(ROOT/'skills/recortex/scripts/observer.py'),'release',hold_id],self.env)
                finally:
                    os._exit(127)
            seen=b''
            try:
                while b'Type release to confirm' not in seen:
                    try:
                        seen+=os.read(fd,4096)
                    except OSError:
                        self.fail(seen)
                os.write(fd,answer)
                while True:
                    try:
                        chunk=os.read(fd,4096)
                    except OSError:
                        break
                    if not chunk:break
                    seen+=chunk
            finally:
                os.close(fd)
            return os.waitpid(pid,0)[1],seen
        status,seen=run(b'no\n')
        self.assertNotEqual(0,status);self.assertIn(b'not-confirmed',seen);self.assertEqual(['active'],self.held())
        status,seen=run(b'release\n')
        self.assertEqual(0,status,seen);self.assertIn(b'"released": true',seen);self.assertEqual(['released'],self.held())
    @unittest.skipIf(observer.inside_claude(),"the user's own terminal is outside every Claude Code session")
    def test_disable_asks_on_the_users_terminal_while_a_hold_applies(self):
        import pty
        self.cli('enable','--model','sonnet','--level','everything','--yes')
        self.hold()
        result,value=self.cli('disable')  # pipes: refused, and the folder stays enabled
        self.assertEqual(1,result.returncode);self.assertEqual('disable_unanswered',value['code'])
        self.assertEqual([os.path.realpath(self.project)],list(self.approvals()))
        pid,fd=pty.fork()
        if pid==0:
            try:
                os.chdir(self.project)
                os.execve(sys.executable,[sys.executable,str(ROOT/'skills/recortex/scripts/observer.py'),'disable'],self.env)
            finally:
                os._exit(127)
        seen=b''
        try:
            while b'Type disable to confirm' not in seen:
                seen+=os.read(fd,4096)
            os.write(fd,b'disable\n')
            while True:
                try:
                    chunk=os.read(fd,4096)
                except OSError:
                    break
                if not chunk:break
                seen+=chunk
        finally:
            os.close(fd)
        self.assertEqual(0,os.waitpid(pid,0)[1],seen);self.assertEqual({},self.approvals())
        self.assertEqual(['active'],self.held())  # the queue and its holds stay
    def test_runtime_check_needs_this_folders_approval(self):
        result,value=self.cli('runtime-check')
        self.assertEqual(1,result.returncode);self.assertEqual('approval',value['code'])
        self.assertFalse((self.base/'state').exists())

if __name__=='__main__':unittest.main()
