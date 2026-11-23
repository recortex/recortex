"""Isolation and pointer integrity; fake runtime output does not measure model quality."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/recortex/scripts'))
import names
import observer_runtime as runtime
import observer_sources as sources
from tests.observer_source_fixture import fixture_trusted_ancestors
SCHEMA = {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'XDG_STATE_HOME':str(self.base),'RECORTEX_OBSERVER':''})
        self.env.start()
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def fake(self, tools=None, output=None):
        rows = [{'type':'system','subtype':'init','tools':['StructuredOutput'] if tools is None else tools,'mcp_servers':[],'skills':[],'plugins':[{'name':'inventory'}]}, {'type':'result','is_error':False,'structured_output':{'ok':True} if output is None else output,'usage':{'input_tokens':4}}]
        script = 'import json; rows='+repr(rows)+'; [print(json.dumps(r)) for r in rows]'
        with patch.object(runtime,'command',return_value=[sys.executable,'-c',script]), patch.object(runtime.shutil,'which',return_value=sys.executable):
            return runtime.invoke({'fictional':True},SCHEMA,'smoke',{})
    def test_isolated_schema_output(self):
        result=self.fake()
        self.assertTrue(result['ok'])
        self.assertEqual(result['usage'],{'input_tokens':4})
    def test_calls_use_the_environment_as_claude_code_does_and_carry_no_dollar_cap(self):
        child = ('import os,json; print(json.dumps(sorted(k for k in os.environ '
                 'if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_") or k == "RECORTEX_OBSERVER")))')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-example', 'CLAUDE_CODE_USE_BEDROCK': '1'}):
            code, output, why = runtime._run([sys.executable, '-c', child], '{}', str(self.base), 5)
            self.assertEqual(['ANTHROPIC_API_KEY', 'CLAUDE_CODE_USE_BEDROCK'], runtime.billing_variables())
        self.assertEqual(['ANTHROPIC_API_KEY', 'CLAUDE_CODE_USE_BEDROCK', 'RECORTEX_OBSERVER'], json.loads(output))
        self.assertNotIn('--max-budget-usd', runtime.command(SCHEMA, {}))
        self.assertEqual([], runtime.billing_variables({'ANTHROPIC_API_KEY': ''}))
    def test_approval_belongs_to_one_machine_and_one_project_folder(self):
        (self.base / 'a').mkdir(); (self.base / 'b').mkdir()
        config = {'observer': {'enabled': True, 'level': 'notes', 'publisher_id': runtime.publisher_id(create=True)}}
        approval = runtime.approval_record('octo-org/octo-repo', config, str(self.base / 'a'))
        self.assertTrue(runtime.approval_valid('octo-org/octo-repo', config, approval, str(self.base / 'a')))
        self.assertFalse(runtime.approval_valid('octo-org/octo-repo', config, approval, str(self.base / 'b')))
        self.assertFalse(runtime.approval_valid('octo-org/other', config, approval, str(self.base / 'a')))
        other = {'observer': dict(config['observer'], publisher_id='0' * 32)}
        self.assertFalse(runtime.approval_valid('octo-org/octo-repo', other,
                                                runtime.approval_record('octo-org/octo-repo', other, str(self.base / 'a')),
                                                str(self.base / 'a')))
        runtime.approve('octo-org/octo-repo', config, str(self.base / 'a'))
        local = runtime.local_config({'repo': 'octo-org/octo-repo', 'observer': {'enabled': True, 'mode': 'automatic'}},
                                     str(self.base / 'a'))
        self.assertTrue(runtime.approval_valid('octo-org/octo-repo', local, root=str(self.base / 'a')))
        self.assertEqual('shadow', local['observer']['mode'])  # the project file cannot widen the settings
        self.assertNotIn('observer', runtime.local_config({'repo': 'octo-org/octo-repo'}, str(self.base / 'b')))
        self.assertNotIn('observer', runtime.local_config({'observer': False}, str(self.base / 'a')))
        self.assertEqual('folder_name', runtime.folder_problem('/tmp/quote"d'))
        self.assertIsNone(runtime.folder_problem(str(self.base / 'a')))
    def test_tool_and_schema_violations(self):
        self.assertEqual(self.fake(tools=['Bash'])['code'],'isolation')
        # A claude that fails or never starts is a failed check, not an isolation finding.
        for script in ('import sys; sys.exit(1)', 'print("not json at all")'):
            with patch.object(runtime,'command',return_value=[sys.executable,'-c',script]), \
                    patch.object(runtime.shutil,'which',return_value=sys.executable):
                self.assertEqual('runtime-failed', runtime.invoke({'fictional':True},SCHEMA,'smoke',{})['code'], script)
        with patch.object(runtime.shutil,'which',return_value=None):
            self.assertEqual('runtime-unavailable', runtime.invoke({'fictional':True},SCHEMA,'smoke',{})['code'])
        self.assertEqual(self.fake(output={'ok':True,'secret':'reject'})['code'],'invalid-output')
        self.assertFalse(runtime.validate(True,{'type':'integer'}))
    def test_gate_and_guard_prevent_launch(self):
        # A claude on PATH, so the gate and the guard are what stop the launch (CI machines have no claude).
        with patch.object(runtime.subprocess,'Popen') as launch, \
                patch.object(runtime.shutil,'which',return_value=sys.executable):
            self.assertEqual(runtime.invoke({},SCHEMA,'sort',{})['code'],'level')  # no model call before a level is chosen
            self.assertEqual(runtime.invoke({},SCHEMA,'sort',{'level':'notes'})['code'],'runtime-gate')
            with patch.dict(os.environ,{'RECORTEX_OBSERVER':'1'}):
                self.assertEqual(runtime.invoke({},SCHEMA,'smoke',{})['code'],'recursion')
            launch.assert_not_called()
    def test_total_input_limit(self):
        with patch.object(runtime.subprocess,'Popen') as launch, \
                patch.object(runtime.shutil,'which',return_value=sys.executable):
            self.assertEqual(runtime.invoke({},SCHEMA,'smoke',{'max_input_chars':10})['code'],'input-limit')
            launch.assert_not_called()
    def test_deadline(self):
        code,_,why=runtime._run([sys.executable,'-c','import time; time.sleep(30)'],'{}',str(self.base),0.1)
        self.assertEqual(why,'timeout')
        self.assertNotEqual(code,0)
    def test_payload_and_eof_reach_child_through_multiple_pipe_writes(self):
        payload=json.dumps({'text':'Invented input. '*2000,'unicode':'\u2603'*3000},ensure_ascii=False)
        child='import sys,json; value=sys.stdin.read(); print(json.dumps({"payload":value,"eof":True}))'
        code,output,why=runtime._run([sys.executable,'-c',child],payload,str(self.base),2)
        self.assertEqual(0,code)
        self.assertIsNone(why)
        self.assertEqual({'payload':payload,'eof':True},json.loads(output))
    def test_pipe_failure_reaps_child(self):
        spawned=[]
        original=subprocess.Popen
        def launch(*a,**kw):
            child=original(*a,**kw); spawned.append(child); return child
        with patch.object(runtime.subprocess,'Popen',side_effect=launch),patch.object(runtime.os,'write',side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                runtime._run([sys.executable,'-c','import time; time.sleep(30)'],'{}',str(self.base),1)
        self.assertIsNotNone(spawned[0].poll())
    def test_output_limit(self):
        with patch.object(runtime,'MAX_OUTPUT',512):
            _,_,why=runtime._run([sys.executable,'-c',"print('x'*10000)"],'{}',str(self.base),2)
        self.assertEqual(why,'output-limit')
    @patch.object(runtime, "fingerprint", return_value="a" * 64)
    def test_signed_gate_bound_to_model_and_minimum_version(self, fingerprint):
        self.assertFalse(runtime.gate_valid(None))
        p={'schema':1,'ok':True,'version':'2.1.325','executable_fingerprint':runtime.fingerprint(),'structured_exact':True,'isolation_checked':True,'recursion_checked':True,'canary_absent':True,'isolation_version':1,'requested_model':'','actual_model':'test-model'}
        self.assertFalse(runtime.gate_valid(p))
        p['mark']=runtime._proof_mark(p,create=True)
        self.assertTrue(runtime.gate_valid(p,{}))
        self.assertFalse(runtime.gate_valid(p,{'model':'changed'}))
        p['requested_model']='opus';p['mark']=runtime._proof_mark(p)
        self.assertTrue(runtime.gate_valid(p,{'model':'opus'}))
        self.assertFalse(runtime.gate_valid(p,{'model':'sonnet'}))
        p['version']='2.1.1';p['mark']=runtime._proof_mark(p)
        self.assertFalse(runtime.gate_valid(p))
    def test_settings_bounds(self):
        self.assertEqual(runtime.settings({'observer':{'max_invocations':900}})['max_invocations'],70)
        for settings in ({'max_invocations':True},{'shell':'bad'}):
            with self.assertRaises(ValueError):runtime.settings({'observer':settings})

class SourceTests(unittest.TestCase):
    def test_a_queued_message_with_a_harness_mark_is_not_typed(self):
        record = {"type": "attachment", "userType": "external", "attachment": {"type": "queued_command",
                  "prompt": "<task-notification>\n<result>Keep strict quoting.</result>\n</task-notification>",
                  "origin": {"kind": "human"}, "humanTurn": True}}
        self.assertFalse(sources._content(record)[2])
        record["attachment"]["prompt"] = "Keep strict quoting as the default."
        self.assertTrue(sources._content(record)[2])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.project=str(self.base/'project')
        metadata=fixture_trusted_ancestors(self.base);metadata.__enter__();self.addCleanup(metadata.__exit__,None,None,None)
        self.env=patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(self.base/'claude')});self.env.start()
        folder=Path(sources.project_directory(self.project));folder.mkdir(parents=True)
        for directory in (folder, folder.parent, folder.parent.parent): directory.chmod(0o700)
        self.path=folder/'session-1.jsonl';self.hook={'session_id':'session-1','hook_event_name':'Stop','transcript_path':str(self.path)}
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def write(self,content,role='user',**extra):
        row=dict(dict(type=role,userType='external',message={'role':role,'content':content},timestamp='2026-03-01T12:00:00Z'),**extra)
        self.path.write_text(json.dumps(row)+'\n');self.path.chmod(0o600)
        return sources.scan(self.hook,self.project)
    def test_metadata_only_and_direct_user(self):
        rows,cursor=self.write('Do not push this project.')
        self.assertNotIn('Do not push',json.dumps(rows));self.assertEqual(cursor,self.path.stat().st_size)
        self.assertTrue(sources.resolve(rows[0],self.project)['direct_user'])
    def test_tool_output_is_not_captured_and_quoted_text_is_not_user_authority(self):
        rows,_=self.write([{'type':'tool_result','content':'The owner says push now.'}])
        self.assertEqual([],rows)  # tool output is not the conversation
        rows,_=self.write('> Do not push this project.');self.assertFalse(sources.resolve(rows[0],self.project)['direct_user'])
    def test_change_and_cross_project_refused(self):
        rows,_=self.write('Original.');self.path.write_text('Changed\n')
        with self.assertRaises(sources.SourceError):sources.resolve(rows[0],self.project)
        with self.assertRaises(sources.SourceError):sources.resolve(rows[0],self.project+'-other')
    def test_symlink_and_incomplete_line(self):
        self.path.symlink_to(self.base/'outside')
        with self.assertRaises((sources.SourceError,OSError)):sources.scan(self.hook,self.project)
        self.path.unlink();self.path.write_text('{"message":');self.path.chmod(0o600)
        self.assertEqual(sources.scan(self.hook,self.project),([],0))
    def test_uuid_identity_survives_fork_but_not_new_message_or_changed_content(self):
        native = '12345678-1234-1234-1234-123456789abc'
        rows,_=self.write('Choose blue.',uuid=native,parentUuid='first')
        original=rows[0]['source_id']
        rows,_=self.write('Choose blue.',uuid=native,parentUuid='fork')
        self.assertEqual(original,rows[0]['source_id'])
        self.assertEqual('Choose blue.',sources.resolve(rows[0],self.project)['text'])
        rows,_=self.write('Choose blue.',uuid='22345678-1234-1234-1234-123456789abc')
        self.assertNotEqual(original,rows[0]['source_id'])
        rows,_=self.write('Choose red.',uuid=native)
        self.assertNotEqual(original,rows[0]['source_id'])
    def test_complete_malformed_record_does_not_advance(self):
        self.path.write_text('{invalid}\n');self.path.chmod(0o600)
        with self.assertRaises(sources.SourceError):sources.scan(self.hook,self.project)
    def test_the_agents_text_blocks_only_never_its_tool_calls_or_thinking(self):
        # A subagent's report reaches the agent, not the user, as a tool call: not the conversation either.
        rows,_=self.write([{'type':'tool_use','name':'SubagentHandback','input':{'message':'Reported outcome.'}}],role='assistant')
        self.assertEqual([],rows)
        rows,_=self.write([{'type':'thinking','thinking':'Private reasoning.'},{'type':'text','text':'All 4 tests pass.'},
                           {'type':'tool_use','id':'c1','name':'Bash','input':{'command':'pytest -q'}},
                           {'type':'text','text':'Shall I commit?'}],role='assistant')
        found=sources.resolve(rows[0],self.project)
        self.assertEqual('All 4 tests pass.\nShall I commit?',found['text']);self.assertFalse(found['direct_user'])
    def test_oversized_record_gaps_progress_to_later_native_message(self):
        big=json.dumps({'type':'user','message':{'role':'user','content':'x'*500}})+'\n'
        good=json.dumps({'type':'user','message':{'role':'user','content':'Later material event.'}})+'\n'
        self.path.write_text(big+good);self.path.chmod(0o600)
        rows=[];cursor=0
        for _ in range(20):
            batch,cursor=sources.scan(self.hook,self.project,cursor=cursor,max_bytes=128)
            rows.extend(batch)
            if cursor==self.path.stat().st_size:break
        self.assertEqual(cursor,self.path.stat().st_size)
        self.assertTrue(any(r['event_type']=='source_gap' for r in rows))
        valid=[r for r in rows if r['event_type']!='source_gap']
        self.assertEqual(1,len(valid));self.assertEqual('Later material event.',sources.resolve(valid[0],self.project)['text'])
        self.assertNotIn('xxx',json.dumps(rows))
    def test_dense_backlog_batches_keep_cursor_after_last_durable_record(self):
        self.path.write_text(''.join(json.dumps({'type':'user','message':{'role':'user','content':'Note '+str(n)}})+'\n' for n in range(1200)))
        self.path.chmod(0o600)
        cursor=0;all_rows=[]
        for _ in range(4):
            rows,new=sources.scan(self.hook,self.project,cursor)
            self.assertLessEqual(len(rows),512);self.assertGreater(new,cursor)
            all_rows.extend(rows);cursor=new
            if cursor==self.path.stat().st_size:break
        self.assertEqual(1200,len(all_rows));self.assertEqual(cursor,self.path.stat().st_size)
    def test_native_authority_change_cannot_share_source_identity(self):
        native='12345678-1234-1234-1234-123456789abc'
        rows,_=self.write('Do not push.',uuid=native);original=rows[0]['source_id']
        rows,_=self.write('Do not push.',uuid=native,isSidechain=True)
        self.assertEqual([],rows)  # a sidechain is not the conversation: it is not captured at all
    def test_preflight_explains_permissions_without_changing_them(self):
        self.path.parent.parent.chmod(0o775)
        with patch.object(sources,'private_group',return_value=False):  # a group shared with another account
            self.assertEqual('source-permissions',sources.preflight(self.project)['code'])
        self.assertEqual(0o775,self.path.parent.parent.stat().st_mode & 0o777)
    def test_a_transcript_truncated_while_its_start_is_found_stops_the_search(self):
        # A transcript truncated or rotated between the size check and the read once made the search for the first
        # whole line loop until the hook's deadline.
        self.path.write_text('x'*5000+'\n');self.path.chmod(0o600)
        original=sources._read;reads=[]
        def shrinking(path,offset,size):
            reads.append(offset)
            if len(reads)>5:raise AssertionError('the search loops on a truncated file')
            with open(path,'r+b') as handle:handle.truncate(10)
            return original(path,offset,size)
        with patch.object(sources,'TAIL_BYTES',1000),patch.object(sources,'_read',side_effect=shrinking):
            with self.assertRaisesRegex(sources.SourceError,'source-truncated'):
                sources.first_cursor(self.hook,self.project)
        self.path.write_text('a'*3000+'\n'+'b'*100+'\n');self.path.chmod(0o600)
        with patch.object(sources,'TAIL_BYTES',1000):
            self.assertEqual(3001,sources.first_cursor(self.hook,self.project))
    def test_a_call_and_its_result_give_no_source_and_no_match(self):
        invoke={'type':'assistant','message':{'role':'assistant','content':[{'type':'tool_use','id':'call1','name':'Read','input':{'file_path':'notes.txt'}}]}}
        result={'type':'user','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'call1','content':'Tests passed in a fictional example.','is_error':False}]}}
        self.path.write_text(json.dumps(invoke)+'\n'+json.dumps(result)+'\n');self.path.chmod(0o600)
        rows,cursor=sources.scan(self.hook,self.project)
        self.assertEqual(([],self.path.stat().st_size),(rows,cursor))
    def test_meta_and_assistant_roles(self):
        rows,_=self.write('Tests passed.','assistant');self.assertEqual(sources.resolve(rows[0],self.project)['role'],'assistant')
    def test_records_outside_the_conversation_are_not_captured(self):
        # Claude Code 2.1.325 writes api-request-blob copies of what it sends, with system reminders (the user's email
        # among them), and adds a skill's text as isMeta: neither is captured, nor are a compaction summary, a line
        # shown only in the transcript, attachments or a record whose type and role disagree.
        reminder='<system-reminder>\nThe user\'s email address is octocat@example.test.\n</system-reminder>'
        for row in ({'type':'api-request-blob','message':{'role':'user','content':[{'type':'text','text':reminder},{'type':'text','text':'Choose blue.'}]}},
                    {'type':'api-request-blob','message':{'role':'assistant','content':[{'type':'text','text':'Done.'}]}},
                    {'type':'user','isMeta':True,'message':{'role':'user','content':'Base directory for this skill.'}},
                    {'type':'user','userType':'external','isSidechain':False,'isCompactSummary':True,'isVisibleInTranscriptOnly':True,
                     'parentUuid':None,'logicalParentUuid':'12345678-1234-1234-1234-123456789abc','uuid':'22345678-1234-1234-1234-123456789abc',
                     'message':{'role':'user','content':'This session is being continued from a previous conversation that ran out of context. The conversation is summarized below:\nThe user said: We will use strict quoting from now on.'}},
                    {'type':'user','isVisibleInTranscriptOnly':True,'message':{'role':'user','content':'Shown only.'}},
                    {'type':'attachment','message':{'role':'user','content':'Attached.'}},
                    {'type':'assistant','message':{'role':'user','content':'Mismatched.'}}):
            self.path.write_text(json.dumps(row)+'\n');self.path.chmod(0o600)
            self.assertEqual([],sources.scan(self.hook,self.project)[0],row)
    def test_claude_codes_own_assistant_records_are_not_the_agents_words(self):
        # An API error message or a synthetic reply is written by Claude Code, not by the agent to the user.
        for extra,model in (({'isApiErrorMessage':True},'claude-opus-5-5'),({},'<synthetic>')):
            row=dict(dict(type='assistant',message={'role':'assistant','model':model,'content':[{'type':'text','text':'API Error: overloaded. All 12 checks pass.'}]}),**extra)
            self.path.write_text(json.dumps(row)+'\n');self.path.chmod(0o600)
            self.assertEqual([],sources.scan(self.hook,self.project)[0],extra or model)
        rows,_=self.write([{'type':'text','text':'All 12 checks pass.'}],role='assistant')
        self.assertEqual('All 12 checks pass.',sources.resolve(rows[0],self.project)['text'])
    def test_system_reminders_never_reach_a_model(self):
        rows,_=self.write([{'type':'text','text':'4 tests pass.\n<system-reminder>\nThe user\'s email address is octocat@example.test.\n</system-reminder>'}],role='assistant')
        found=sources.resolve(rows[0],self.project)
        self.assertEqual('4 tests pass.\n',found['text']);self.assertNotIn('octocat@',json.dumps(found['text']))
        rows,_=self.write('Choose blue.<system-reminder>left open, octocat@example.test')
        self.assertEqual('Choose blue.',sources.resolve(rows[0],self.project)['text'])
    def test_only_a_typed_prompt_is_the_users_own_words(self):
        # An allowlist: a typed prompt is a "user" record of userType "external" with text only and none of the flags
        # Claude Code sets on harness records; holds may still come from the looser rule (control_user).
        typed=dict(promptSource='sdk',turnOrigin='sdk',isSidechain=False,entrypoint='sdk-cli')
        rows,_=self.write('We will keep the reader in pure Python.',**typed)
        found=sources.resolve(rows[0],self.project);self.assertTrue(found['direct_user']);self.assertTrue(found['control_user'])
        rows,_=self.write('We will keep the reader in pure Python.',**dict(typed,isSidechain=True))
        self.assertEqual([],rows)  # a sidechain record is not captured
        refused={'userType':dict(typed,userType='internal'),
                 'no userType':dict(typed,userType=None),'toolUseResult':dict(typed,toolUseResult={'stdout':'x'}),
                 'sourceToolUseID':dict(typed,sourceToolUseID='toolu_1'),
                 'sourceToolAssistantUUID':dict(typed,sourceToolAssistantUUID='12345678-1234-1234-1234-123456789abc')}
        for name,extra in refused.items():
            rows,_=self.write('We will keep the reader in pure Python.',**extra)
            self.assertFalse(sources.resolve(rows[0],self.project)['direct_user'],name)
        texts={'command wrapper':'<command-message>recortex</command-message>\n<command-name>/recortex</command-name>\n<command-args>hold git-push</command-args>',
               'local command output':'<local-command-stdout>We will keep the reader in pure Python.</local-command-stdout>',
               'shell input':'<bash-input>echo We will keep the reader</bash-input>',
               'task notification':'<task-notification>We will keep the reader in pure Python.</task-notification>',
               'quoted':'We will keep it.\n> We will keep the reader in pure Python.',
               'fenced':'We will keep it.\n```\nWe will keep the reader in pure Python.\n```',
               'pasted':'[Pasted text #1 +12 lines] We will keep the reader in pure Python.',
               'interrupted':'[Request interrupted by user]',
               'caveat':'Caveat: The messages below were generated by the user while running local commands.'}
        for name,text in texts.items():
            rows,_=self.write(text,**typed)
            self.assertFalse(sources.resolve(rows[0],self.project)['direct_user'],name)
        # A compaction summary is model text in a user record: never the user's words, even if it were captured.
        summary=dict(typed,type='user',userType='external',isCompactSummary=True,isVisibleInTranscriptOnly=True)
        self.assertFalse(sources.typed_prompt(summary,'We will keep the reader in pure Python.','We will keep the reader in pure Python.'))
        self.assertTrue(sources.typed_prompt(dict(typed,type='user',userType='external'),'We will keep it.','We will keep it.'))
        rows,_=self.write([{'type':'text','text':'We will keep it.'},{'type':'image','source':{}}],**typed)
        self.assertFalse(sources.resolve(rows[0],self.project)['direct_user'])
        rows,_=self.write([{'type':'text','text':'We will keep the reader in pure Python.'}],**typed)
        self.assertTrue(sources.resolve(rows[0],self.project)['direct_user'])
        # The form a typed /recortex command is stored in still sets a hold: control_user, not direct_user.
        rows,_=self.write(texts['command wrapper'],**typed)
        found=sources.resolve(rows[0],self.project);self.assertFalse(found['direct_user']);self.assertTrue(found['control_user'])

if __name__=='__main__':unittest.main()
