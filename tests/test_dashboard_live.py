"""The live dashboard server (1.8.0): its two keys, its per-project routing, its lifetime, the ping that keeps it alive, the
mark that drives the page's reloads, the hook's part and the guard. All offline: the server runs in the test's own process on
a free loopback port (or as a subprocess with the sandbox's folders), and no test contacts GitHub."""

import contextlib
import http.client
import html
import io
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_jobs_tasks as tj
import test_scripts as ts
sys.path.insert(0, ts.SCRIPTS)
import dashboard_action as action
import dashboard_notes
import dashboard_server
import hooks
import mind
import pretool_guard
import recortex
import storage

MOUNT, VIEW, CHANGE = "a" * 32, "b" * 32, "c" * 32
STEP = "7:9108:2"
WORDS = "I reran the conformance set and all twelve files passed."


class LiveBox(tj.TaskBox):
    """The task record with a server in this process, one project registered, and calls over a real socket."""

    def setUp(self):
        super().setUp()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project, "SSH_CONNECTION": None, "SSH_CLIENT": None, "SSH_TTY": None,
                               "TERM_PROGRAM": None, "VSCODE_IPC_HOOK_CLI": None, "RECORTEX_DASHBOARD": None})
        self.server = action.Server(("127.0.0.1", 0), action.Handler, MOUNT, VIEW, CHANGE)
        self.server.register(self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.ident = dashboard_server.project_id(self.project)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def cookies(self, level):
        return {"view": "recortex_view=%s" % VIEW, "change": "recortex_view=%s; recortex_change=%s" % (VIEW, CHANGE),
                None: ""}[level]

    def call(self, method, path, data=None, level="change", host=None, extra=None, project=True, mount=True):
        """(status, headers, text). `path` is inside the project unless project is False; mount False leaves the prefix off."""
        port = self.server.server_port
        headers = {"Host": host if host is not None else "127.0.0.1:%d" % port}
        if self.cookies(level):
            headers["Cookie"] = self.cookies(level)
        body = None
        if method == "POST":
            body = json.dumps(data if data is not None else {})
            headers.update({"Content-Type": "application/json", "Origin": "http://" + headers["Host"]})
        headers.update(extra or {})
        prefix = ("/" + MOUNT if mount else "") + ("/p/%s" % self.ident if project else "")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            connection.request(method, prefix + path, body=body, headers=headers)
            answer = connection.getresponse()
            return answer.status, dict(answer.getheaders()), answer.read().decode("utf-8")
        finally:
            connection.close()

    def comments(self):
        return len(self.repo()["comments"])

    def mark(self):
        status, _, text = self.call("GET", "/api/changed")
        self.assertEqual(200, status, text)
        return json.loads(text)["mark"]

    def second_project(self):
        """A second project folder with the same project file, so a second id."""
        second = os.path.join(self.dir, "project-two")
        os.makedirs(second)
        with open(os.path.join(self.project, ".recortex.json"), encoding="utf-8") as handle, \
                open(os.path.join(second, ".recortex.json"), "w", encoding="utf-8") as out:
            out.write(handle.read())
        self.server.register(second)
        return second, dashboard_server.project_id(second)


class KeyTests(LiveBox):
    def test_the_view_key_opens_no_post_at_any_address(self):
        before = self.comments()
        for path in action.POSTS:
            with self.subTest(path=path):
                status, _, text = self.call("POST", path, {}, level="view")
                self.assertEqual((403, {"code": "unauthorized"}), (status, json.loads(text)))
        status, _, _ = self.call("POST", "/api/stop", {}, level="view")
        self.assertEqual(403, status)
        self.assertFalse(self.server.stop_requested)
        self.assertEqual(before, self.comments())

    def test_the_change_key_reaches_the_actions_the_1_7_1_tests_exercise(self):
        status, _, text = self.call("POST", "/api/preview", {"step": STEP, "words": WORDS})
        self.assertEqual(200, status, text)
        self.assertIn("body_hash", json.loads(text))

    def test_without_a_key_a_page_of_a_record_is_refused_and_outside_the_prefix_is_empty(self):
        for path in ("/snapshot", "/api/state", "/api/changed"):
            with self.subTest(path=path):
                self.assertEqual(403, self.call("GET", path, level=None)[0])
        self.assertEqual(200, self.call("GET", "/", level=None)[0])   # the shell page holds no record
        self.assertEqual(200, self.call("GET", "/suggestions", level=None)[0])
        status, _, text = self.call("GET", "/snapshot", mount=False, project=False, level="change")
        self.assertEqual((404, ""), (status, text))
        status, _, text = self.call("GET", "/api/projects", mount=False, project=False, level="change")
        self.assertEqual((404, ""), (status, text))
        status, _, text = self.call("GET", "/", mount=False, project=False, level="change")
        self.assertEqual((404, ""), (status, text))

    def test_the_session_sets_the_cookie_the_key_deserves_and_nothing_for_a_wrong_key(self):
        def session(key):
            return self.call("POST", "/api/session", {}, level=None, extra={"Authorization": "Bearer " + key})

        status, headers, _ = session(VIEW)
        self.assertEqual(200, status)
        cookie = headers["Set-Cookie"]
        self.assertIn("recortex_view=" + VIEW, cookie)
        self.assertNotIn("recortex_change", cookie)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/%s/" % MOUNT):
            self.assertIn(flag, cookie)
        status, headers, text = session(CHANGE)
        self.assertEqual((200, True), (status, json.loads(text)["change"]))
        self.assertIn("recortex_change=" + CHANGE, headers["Set-Cookie"])
        for key in ("d" * 32, "", VIEW[:-1], CHANGE + "0"):
            self.assertEqual(403, session(key)[0], key)

    def test_a_view_page_says_live_view_and_holds_no_action_address_the_change_page_does(self):
        status, _, view = self.call("GET", "/snapshot", level="view")
        self.assertEqual(200, status)
        self.assertIn("Live view.", view)
        self.assertNotIn("Local interactive session.", view)
        self.assertNotIn("Static snapshot.", view)
        for address in ("/actions", "/map-editor", "/suggestions", "/impact-review"):
            self.assertNotIn(address, view)
        status, _, change = self.call("GET", "/snapshot", level="change")
        self.assertIn("Local interactive session.", change)
        self.assertNotIn("Static snapshot.", change)
        self.assertIn("/%s/p/%s/actions" % (MOUNT, self.ident), change)
        for page in (view, change):
            self.assertIn("connect-src 'self'", html.unescape(page))
            self.assertNotIn(CHANGE, page)
            self.assertNotIn(VIEW, page)

    def test_host_and_origin_pass_only_for_the_loopback_names(self):
        for host in ("127.0.0.1", "127.0.0.1:47123", "localhost", "localhost:80"):
            with self.subTest(host=host):
                self.assertTrue(action.origin_ok({"Host": host, "Origin": "http://" + host}), host)
        for host in ("evil.example", "127.0.0.1.evil.example", "localhost.evil.example:80", "[::1]:80", "", "127.0.0.1:x",
                     "127.0.0.1:1234567"):
            with self.subTest(host=host):
                self.assertFalse(action.origin_ok({"Host": host}), host)
        self.assertFalse(action.origin_ok({"Host": "127.0.0.1:80", "Origin": "http://localhost:80"}))
        self.assertFalse(action.origin_ok({"Host": "127.0.0.1:80", "Origin": "http://evil.example"}))
        self.assertFalse(action.origin_ok({"Host": "127.0.0.1:80", "Sec-Fetch-Site": "cross-site"}))
        self.assertEqual(403, self.call("GET", "/snapshot", host="evil.example")[0])
        self.assertEqual(403, self.call("GET", "/snapshot", host="127.0.0.1.evil.example:80")[0])
        port = self.server.server_port
        self.assertEqual(403, self.call("POST", "/api/preview", {"step": STEP, "words": WORDS},
                                        extra={"Origin": "http://localhost:%d" % port})[0])


class OursTests(LiveBox):
    def hook_body(self, root=None, at=None, nonce=None, mac=None):
        root = os.path.realpath(root or self.project)
        at = int(time.time()) if at is None else at
        nonce = nonce or os.urandom(16).hex()
        return {"root": root, "at": at, "nonce": nonce, "mac": mac or dashboard_server.hook_mac(root, at, nonce)}

    def send(self, body):
        port = self.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST", "/%s/api/hook" % MOUNT, body=json.dumps(body),
                               headers={"Host": "127.0.0.1:%d" % port, "Content-Type": "application/json"})
            answer = connection.getresponse()
            return answer.status, answer.read().decode("utf-8")
        finally:
            connection.close()

    def test_a_ping_with_the_right_mac_gets_the_pong_and_the_server_knows_the_project(self):
        body = self.hook_body()
        status, text = self.send(body)
        self.assertEqual(200, status, text)
        self.assertEqual(dashboard_server.pong_mac(body["nonce"]), json.loads(text)["pong"])

    def test_a_wrong_mac_an_old_time_or_a_replayed_nonce_gets_no_pong(self):
        wrong = self.hook_body(mac="0" * 64)
        old = self.hook_body(at=int(time.time()) - 3600)
        other = self.hook_body(root="/somewhere/else")
        other["root"] = os.path.realpath(self.project)   # a mac made for another folder
        for body in (wrong, old, other):
            status, text = self.send(body)
            self.assertEqual(403, status)
            self.assertNotIn("pong", text)
        fresh = self.hook_body()
        self.assertEqual(200, self.send(fresh)[0])
        status, text = self.send(fresh)
        self.assertEqual(403, status)
        self.assertNotIn("pong", text)
        self.assertEqual(400, self.send({"root": self.project})[0])

    def test_a_ping_for_an_untrusted_project_is_answered_but_the_project_is_not_registered(self):
        outside = os.path.join(self.dir, "unconfigured")
        os.makedirs(outside)
        self.assertEqual(200, self.send(self.hook_body(root=outside))[0])
        self.assertNotIn(dashboard_server.project_id(outside), self.server.projects)

    def test_the_server_answers_a_hook_ping_made_by_dashboard_server_ping(self):
        state = {"port": self.server.server_port, "mount": MOUNT}
        self.assertEqual(("running", dashboard_server.version()), dashboard_server.ping(state, self.project, 2.0))

    def test_a_listener_that_answers_anything_but_the_right_pong_is_stale_and_gets_no_key(self):
        received = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                received.append(self.request.recv(65536))
                self.request.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n'
                                     b'{"pong": "' + b"f" * 64 + b'", "version": "' + dashboard_server.version().encode() + b'"}')

        listener = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        state = {"form": 1, "pid": os.getpid(), "port": listener.server_address[1], "mount": "d" * 32, "view": VIEW,
                 "change": CHANGE, "version": dashboard_server.version(), "started": int(time.time())}
        dashboard_server.write_state(state)
        with mock.patch.object(dashboard_server, "start") as start:
            self.assertEqual("started", dashboard_server.ensure(self.project))
        start.assert_called_once()
        self.assertEqual(("stale", ""), dashboard_server.ping(state, self.project, 2.0))
        self.assertTrue(received)
        for request in received:
            self.assertNotIn(VIEW.encode(), request)
            self.assertNotIn(CHANGE.encode(), request)

    def test_a_state_file_of_another_host_name_is_not_read_and_one_that_does_not_fit_is_stale(self):
        state = {"form": 1, "pid": os.getpid(), "port": 47001, "mount": MOUNT, "view": VIEW, "change": CHANGE,
                 "version": "1.8.0", "started": int(time.time())}
        with mock.patch.object(socket, "gethostname", return_value="login-1"):
            dashboard_server.write_state(state)
            first = dashboard_server.state_name()
            self.assertEqual(state, dashboard_server.read_state())
        with mock.patch.object(socket, "gethostname", return_value="login-2"):
            self.assertIsNone(dashboard_server.read_state())
            self.assertNotEqual(first, dashboard_server.state_name())
            self.assertNotEqual("dashboard-server-login-2.lock", dashboard_server.lock_name())
        for broken in (dict(state, port=0), dict(state, port=70000000000), dict(state, extra=1), dict(state, form=2),
                       dict(state, view="x" * 32), dict(state, mount="A" * 32), dict(state, pid="1"),
                       {key: value for key, value in state.items() if key != "change"}):
            with self.subTest(broken=sorted(broken)), mock.patch.object(socket, "gethostname", return_value="login-3"):
                dashboard_server.write_state(broken)
                self.assertIsNone(dashboard_server.read_state())

    def test_the_state_folder_is_the_private_one_and_the_files_are_private(self):
        dashboard_server.write_state({"form": 1, "pid": 1, "port": 47001, "mount": MOUNT, "view": VIEW, "change": CHANGE,
                                      "version": "1.8.0", "started": int(time.time())})
        folder = dashboard_server.private_folder()
        self.assertEqual(os.path.join(storage.state_top(for_key=True), "private"), folder)
        path = os.path.join(folder, dashboard_server.state_name())
        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
        self.assertEqual(0o700, os.stat(folder).st_mode & 0o777)


class LifetimeTests(LiveBox):
    def test_it_serves_at_23_hours_after_the_last_ping_stops_at_24_and_only_a_ping_extends_it(self):
        moment = [1000.0]
        self.server.clock = lambda: moment[0]
        self.server.last_ping = moment[0]
        # a separate server object serves in this test, so that the thread of LiveBox is left alone
        second = action.Server(("127.0.0.1", 0), action.Handler, MOUNT, VIEW, CHANGE)
        second.clock = lambda: moment[0]
        second.last_ping = moment[0]
        second.register(self.project)
        result = []
        thread = threading.Thread(target=lambda: result.append(action.serve(second, tick=0.02)), daemon=True)
        thread.start()
        self.addCleanup(second.server_close)

        def call(method, path, data=None, level="change"):
            headers = {"Host": "127.0.0.1:%d" % second.server_port, "Cookie": self.cookies(level)}
            body = None
            if method == "POST":
                body = json.dumps(data)
                headers.update({"Content-Type": "application/json", "Origin": "http://" + headers["Host"]})
            connection = http.client.HTTPConnection("127.0.0.1", second.server_port, timeout=20)
            try:
                connection.request(method, "/%s%s" % (MOUNT, path), body=body, headers=headers)
                return connection.getresponse().status
            finally:
                connection.close()

        moment[0] = 1000.0 + 23 * 3600
        self.assertEqual(200, call("GET", "/p/%s/api/changed" % self.ident))   # a page request: no extension
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())
        self.assertEqual(1000.0, second.last_ping)
        body = {"root": os.path.realpath(self.project), "at": int(time.time()), "nonce": "9" * 32}
        body["mac"] = dashboard_server.hook_mac(body["root"], body["at"], body["nonce"])
        self.assertEqual(200, call("POST", "/api/hook", body))
        self.assertEqual(moment[0], second.last_ping)   # a ping did
        moment[0] += 23 * 3600
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())
        moment[0] += 2 * 3600
        thread.join(5)
        self.assertEqual(["idle"], result)

    def test_the_launch_folder_is_looked_for_at_each_tick_and_the_day_runs_on_the_wall_clock(self):
        second = action.Server(("127.0.0.1", 0), action.Handler, MOUNT, VIEW, CHANGE)
        self.addCleanup(second.server_close)
        self.assertIs(time.time, second.clock)   # a laptop's sleep counts toward the 24 hours
        self.assertIsNone(action.launch_folder(create=False))   # the folder does not exist when the server starts
        thread = threading.Thread(target=lambda: action.serve(second, tick=0.02), daemon=True)
        thread.start()
        time.sleep(0.2)
        folder = action.launch_folder()   # the first `open` makes it, after the server started
        old = os.path.join(folder, "launch-00000000.html")
        storage.write_file(old, "x")
        os.utime(old, (time.time() - 120, time.time() - 120))
        deadline = time.time() + 10
        while os.path.lexists(old) and time.time() < deadline:
            time.sleep(0.05)
        second.stop_requested = True
        thread.join(5)
        self.assertFalse(os.path.lexists(old))   # removed by the loop, with no `open` to do it

    def test_a_real_server_stops_on_a_valid_stop_and_leaves_no_state_and_a_free_lock(self):
        done = []
        quiet = mock.patch.object(sys, "stdout", io.StringIO())   # the server says "was stopped" when it ends
        quiet.start()
        self.addCleanup(quiet.stop)
        thread = threading.Thread(target=lambda: done.append(action.serve_command(types.SimpleNamespace(port=0))), daemon=True)
        thread.start()
        deadline = time.time() + 10
        state = None
        while state is None and time.time() < deadline:
            state = dashboard_server.read_state()
            time.sleep(0.05)
        self.assertIsNotNone(state)
        self.assertEqual(os.getpid(), state["pid"])
        self.assertIsNone(dashboard_server.take_lock())   # the running server holds it
        self.assertEqual("running", dashboard_server.ping(state, self.project, 3.0)[0])
        self.assertEqual("running", dashboard_server.ping(state, self.project, 3.0, stop=True)[0])
        thread.join(10)
        self.assertEqual([0], done)
        self.assertIsNone(dashboard_server.read_state())
        descriptor = dashboard_server.take_lock()
        self.assertIsNotNone(descriptor)
        os.close(descriptor)

    def test_a_server_of_another_version_is_stopped_and_replaced(self):
        main = threading.current_thread()
        real = dashboard_server.version
        with mock.patch.object(dashboard_server, "version", side_effect=lambda: "1.0.0" if threading.current_thread() is not main else real()):
            state = {"port": self.server.server_port, "mount": MOUNT}
            self.assertEqual(("other version", "1.0.0"), dashboard_server.ping(state, self.project, 2.0))
            dashboard_server.write_state({"form": 1, "pid": os.getpid(), "port": self.server.server_port, "mount": MOUNT,
                                          "view": VIEW, "change": CHANGE, "version": "1.0.0", "started": int(time.time())})
            with mock.patch.object(dashboard_server, "start") as start:
                self.assertEqual("started", dashboard_server.ensure(self.project))
        start.assert_called_once()
        self.assertTrue(self.server.stop_requested)

    def test_after_a_killed_server_ensure_starts_a_new_one_at_once(self):
        script = os.path.join(ts.SCRIPTS, "dashboard_action.py")
        child = subprocess.Popen([sys.executable, script, "serve", "--port", "0"], env=dict(os.environ),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.time() + 15
        state = None
        while state is None and time.time() < deadline:
            state = dashboard_server.read_state()
            time.sleep(0.05)
        self.assertIsNotNone(state)
        self.assertEqual(child.pid, state["pid"])
        self.assertEqual("running", dashboard_server.ensure(self.project))
        os.kill(child.pid, signal.SIGKILL)
        child.wait(10)
        with mock.patch.object(dashboard_server, "start") as start:
            self.assertEqual("started", dashboard_server.ensure(self.project))
        start.assert_called_once()
        descriptor = dashboard_server.take_lock()   # a killed process holds nothing
        self.assertIsNotNone(descriptor)
        os.close(descriptor)

    def test_a_second_server_on_the_same_host_exits_without_a_port_or_a_state_of_its_own(self):
        descriptor = dashboard_server.take_lock()
        self.addCleanup(os.close, descriptor)
        before = dashboard_server.read_state()
        self.assertEqual(0, action.serve_command(types.SimpleNamespace(port=0)))
        self.assertEqual(before, dashboard_server.read_state())

    def test_the_ports_are_47000_plus_the_user_id_modulo_1000_then_the_next_twenty_then_any(self):
        found = dashboard_server.ports(uid=1234)
        self.assertEqual(list(range(47234, 47254)) + [0], found)
        self.assertEqual(47000, dashboard_server.ports(uid=0)[0])
        self.assertEqual(47000, dashboard_server.ports(uid=1000)[0])


class ProjectTests(LiveBox):
    def test_each_id_serves_its_own_previews_only(self):
        second, other = self.second_project()
        status, _, text = self.call("POST", "/api/preview", {"step": STEP, "words": WORDS})
        self.assertEqual(200, status, text)
        made = json.loads(text)["id"]
        before = self.comments()
        saved, self.ident = self.ident, other
        try:
            status, _, text = self.call("POST", "/api/commit", {"id": made, "body_hash": "0" * 64})
        finally:
            self.ident = saved
        self.assertNotEqual(200, status, text)
        self.assertEqual(before, self.comments())
        self.assertNotEqual(self.server.actions[saved].folder, self.server.actions[other].folder)

    def test_a_project_that_leaves_the_trusted_list_disappears_and_is_refused(self):
        second, other = self.second_project()
        status, _, text = self.call("GET", "/api/projects", project=False)
        self.assertEqual(200, status, text)
        self.assertEqual({self.ident, other}, {row["id"] for row in json.loads(text)})
        for row in json.loads(text):
            self.assertEqual({"id", "repo"}, set(row))
        self.assertEqual(200, self.call("GET", "/api/state")[0])
        with open(self.trusted, "w", encoding="utf-8") as handle:
            handle.write("")
        status, _, text = self.call("GET", "/api/state")   # checked again at this request, before anything is read
        self.assertEqual((404, {"code": "unavailable"}), (status, json.loads(text)))
        status, _, text = self.call("GET", "/api/projects", project=False)
        self.assertEqual([], json.loads(text))

    def test_a_project_whose_file_names_another_repository_is_refused(self):
        path = os.path.join(self.project, ".recortex.json")
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual(200, self.call("GET", "/api/state")[0])
        config["repo"] = "octo-org/another-repo"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        status, _, text = self.call("GET", "/api/state")
        self.assertEqual((404, {"code": "unavailable"}), (status, json.loads(text)))
        self.assertEqual([], json.loads(self.call("GET", "/api/projects", project=False)[2]))

    def test_an_unknown_id_is_unavailable_and_says_nothing_about_why(self):
        saved, self.ident = self.ident, "f" * 16
        try:
            status, _, text = self.call("GET", "/api/state")
        finally:
            self.ident = saved
        self.assertEqual((404, {"code": "unavailable"}), (status, json.loads(text)))

    def test_hostile_text_in_a_record_is_escaped_and_never_in_the_list_or_the_landing_page(self):
        data = self.repo()
        data["issues"]["5"]["title"] = "<script>alert(1)</script> hostile"
        self.save(data)
        self.ready()
        second, other = self.second_project()
        status, _, listing = self.call("GET", "/api/projects", project=False)
        status_landing, _, landing = self.call("GET", "/", project=False)
        self.assertEqual((200, 200), (status, status_landing))
        for text in (listing, landing):
            self.assertNotIn("hostile", text)
            self.assertNotIn("<script>alert", text)
        _, _, page = self.call("GET", "/snapshot", level="view")
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_the_landing_page_lists_projects_by_repository_name_and_links_to_each_snapshot(self):
        second, other = self.second_project()
        status, _, landing = self.call("GET", "/", project=False)
        self.assertEqual(200, status)
        self.assertEqual(2, landing.count("octo-org/octo-repo"))
        for ident in (self.ident, other):
            self.assertIn('href="p/%s/snapshot"' % ident, landing)
        self.assertNotIn(CHANGE, landing + VIEW)


class LiveTests(LiveBox):
    def test_the_mark_moves_for_a_sync_a_queue_write_a_note_and_an_action_and_not_otherwise(self):
        first = self.mark()
        self.assertEqual(first, self.mark())
        self.assertRegex(first, r"[0-9a-f]{16}")
        self.post(9301, 5, "A later comment that a sync reads into the index.")
        after_sync = self.mark()
        self.assertNotEqual(first, after_sync)
        self.assertEqual(after_sync, self.mark())
        queue = os.path.join(storage.folder(storage.state_top(), "octo-org__octo-repo", "observer"), "pending.sqlite-wal")
        with open(queue, "wb"):
            pass   # an empty write-ahead file: the queue's own database is left as it is
        after_queue = self.mark()
        self.assertNotEqual(after_sync, after_queue)
        self.assertTrue(dashboard_notes.add("octo-org/octo-repo", "step", 7))
        after_note = self.mark()
        self.assertNotEqual(after_queue, after_note)
        self.assertEqual(200, self.call("POST", "/api/preview", {"step": STEP, "words": WORDS})[0])
        self.assertNotEqual(after_note, self.mark())

    def test_the_view_key_and_the_change_key_both_read_the_mark_and_a_wrong_one_does_not(self):
        self.assertEqual(200, self.call("GET", "/api/changed", level="view")[0])
        self.assertEqual(200, self.call("GET", "/api/changed", level="change")[0])
        self.assertEqual(403, self.call("GET", "/api/changed", level=None)[0])

    def test_the_page_is_built_once_for_each_mark(self):
        built = []
        real = action.Actions.snapshot

        def counting(actions, *args, **kwargs):
            built.append(kwargs.get("interactive"))
            return real(actions, *args, **kwargs)

        with mock.patch.object(action.Actions, "snapshot", counting):
            first = self.call("GET", "/snapshot", level="view")[2]
            again = self.call("GET", "/snapshot", level="view")[2]
            self.assertEqual(first, again)
            self.assertEqual([False], built)
            self.call("GET", "/snapshot", level="change")
            self.assertEqual([False, True], built)
            self.post(9302, 5, "Another comment that changes the index.")
            self.call("GET", "/snapshot", level="view")
            self.assertEqual([False, True, False], built)

    def test_the_page_cache_follows_who_may_be_told_as_well_as_the_mark(self):
        built = []
        real = action.Actions.snapshot

        def counting(actions, *args, **kwargs):
            built.append(1)
            return real(actions, *args, **kwargs)

        with mock.patch.object(action.Actions, "snapshot", counting):
            self.call("GET", "/snapshot", level="view")
            self.call("GET", "/snapshot", level="view")
            self.assertEqual(1, len(built))   # the same mark, the same accounts: served again
            with open(self.trusted, "w", encoding="utf-8") as handle:   # an account that is not the owner leaves, another comes
                handle.write("octo-org/octo-repo hubot\n")
            self.call("GET", "/snapshot", level="view")
            self.assertEqual(2, len(built))   # the page that was built for the earlier accounts is not served

    def test_the_live_script_holds_none_of_the_forbidden_calls_and_the_page_test_knows_it(self):
        with open(os.path.join(ROOT, "skills", "recortex", "page", "live.js"), encoding="utf-8") as handle:
            script = handle.read()
        banned = [word for word in ts.SCRIPT_BANNED if word in script and word != "fetch("]
        self.assertEqual([], banned)
        self.assertEqual(1, script.count("fetch("))
        self.assertIn("location.reload()", script)
        for forbidden in ("innerHTML", "WebSocket", "EventSource", "localStorage", "document.cookie", "eval("):
            self.assertNotIn(forbidden, script)
        self.assertIn("api/changed", script)
        self.assertIn("5000", script)
        self.assertIn("The live dashboard stopped; this page is the last copy.", script)

    def test_the_static_page_is_the_same_bytes_and_policy_with_part_d_in_the_code(self):
        built = self.static_model()
        page = mind.render(built)
        self.assertIn("connect-src 'none'", html.unescape(page))
        self.assertNotIn("connect-src 'self'", html.unescape(page))
        self.assertIn("Static snapshot.", page)
        self.assertNotIn("Live view.", page)
        self.assertNotIn("Projects", page)
        self.assertNotIn("live-stopped", page)
        self.assertEqual(1, page.count("<script>"))
        self.assertEqual(page, mind.render(dict(built, live=False)))
        live = mind.render(dict(built, live=True))
        self.assertEqual(2, live.count("<script>"))
        self.assertNotEqual(page, live)
        self.assertIn("connect-src 'self'", html.unescape(live))
        self.assertIn("Live view.", live)
        self.assertIn(">Projects<", live)

    def static_model(self):
        with action.dashboard_server.project(self.project):
            actions = action.Actions()
            return actions.project_model()


class StateBox(LiveBox):
    def make_state(self):
        """The state file of the server that this test runs, as the real server writes it."""
        state = {"form": 1, "pid": os.getpid(), "port": self.server.server_port, "mount": MOUNT, "view": VIEW,
                 "change": CHANGE, "version": dashboard_server.version(), "started": int(time.time())}
        dashboard_server.write_state(state)
        return state


class HookTests(StateBox):
    AS_CLAUDE_STARTS_IT = [("bash", ["bash"]), ("claude", ["claude"])]   # the one claude above a hook, and no tool shell

    def run_hook(self, event, data, source="startup", parents=None):
        out = io.StringIO()
        stdin = io.StringIO(json.dumps(dict({"source": source, "session_id": "s" * 8}, **data)))
        # the hook's budget counts from the process start; this process has run other tests, so the count starts now. The
        # ancestry is the one Claude Code gives a hook unless the test names another.
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(hooks, "STARTED", time.monotonic()), \
                mock.patch("observer_grants.ancestors", return_value=self.AS_CLAUDE_STARTS_IT if parents is None else parents):
            hooks.main([event])
        return out.getvalue()

    def test_the_session_start_payload_gives_the_change_link_in_systemMessage_only(self):
        self.make_state()
        out = self.run_hook("session-start", {})
        payload = json.loads(out)
        link = "http://127.0.0.1:%d/%s/p/%s/#%s" % (self.server.server_port, MOUNT, self.ident, CHANGE)
        self.assertIn("recortex dashboard: " + link, payload["systemMessage"])
        context = payload["hookSpecificOutput"]["additionalContext"]
        for secret in (CHANGE, VIEW, "127.0.0.1:%d" % self.server.server_port, MOUNT):
            self.assertNotIn(secret, context)
        self.assertNotIn("recortex dashboard:", context)
        self.assertEqual(1, out.count(CHANGE))   # once, in systemMessage

    def test_the_prompt_hook_shows_the_link_for_the_dashboard_command_only_and_only_to_the_user(self):
        self.make_state()
        for prompt in ("what changed", "/recortex:map", "please run /recortex:dashboard now", "/recortex:dashboardx"):
            with self.subTest(prompt=prompt):
                self.assertNotIn(CHANGE, self.run_hook("prompt-submit", {"prompt": prompt}))
        for prompt in ("/recortex:dashboard", "/recortex:dashboard suggestions", "  /recortex:dashboard\n"):
            with self.subTest(prompt=prompt):
                out = self.run_hook("prompt-submit", {"prompt": prompt})
                payload = json.loads(out)
                self.assertIn(CHANGE, payload["systemMessage"])
                self.assertNotIn(CHANGE, payload["hookSpecificOutput"].get("additionalContext", ""))
        out = self.run_hook("prompt-submit", {"prompt": "/recortex:dashboard", "agent_id": "a1"})
        self.assertNotIn(CHANGE, out)

    def test_unless_claude_code_started_it_the_hook_gives_no_change_link_and_starts_nothing(self):
        self.make_state()
        by_hand = [("python3", ["python3", "hooks.py", "session-start"]), ("zsh", ["zsh"])]
        two_claudes = [("bash", ["bash"]), ("claude", ["claude"]), ("claude", ["claude", "-p"])]
        tool_shell = [("bash", ["bash", "-c", "source /home/u/.claude/shell-snapshots/s.sh && eval 'python3 hooks.py'"]),
                      ("claude", ["claude"])]
        for parents in (by_hand, two_claudes, tool_shell, []):
            with self.subTest(parents=parents), mock.patch.object(dashboard_server, "start") as start:
                for event, data in (("session-start", {}), ("prompt-submit", {"prompt": "/recortex:dashboard"})):
                    out = self.run_hook(event, data, parents=parents)
                    for secret in (CHANGE, VIEW, MOUNT, "recortex dashboard:", "127.0.0.1:%d" % self.server.server_port):
                        self.assertNotIn(secret, out, (event, secret))
                start.assert_not_called()
        self.assertIn(CHANGE, self.run_hook("session-start", {}))   # started the way Claude Code starts it: the link is shown
        self.assertIn(CHANGE, self.run_hook("prompt-submit", {"prompt": "/recortex:dashboard"}))

    def test_a_start_that_fails_gives_one_fixed_line_in_the_context_and_the_doctor_says_why(self):
        with mock.patch.object(dashboard_server, "start", side_effect=OSError("no python")):
            out = self.run_hook("session-start", {})
        self.assertIn(hooks.LIVE_FAILED, out)
        self.assertEqual(1, out.count(hooks.LIVE_FAILED))
        self.assertNotIn("no python", out)
        dashboard_server.note_failure("port")
        lines = hooks.machine_summary("octo-org/octo-repo", recortex.load_config(), [])
        self.assertIn("  live dashboard: could not be started: port", lines)
        dashboard_server.clear_failure()
        self.assertEqual("", dashboard_server.read_failure())

    def test_the_doctor_line_for_each_state(self):
        def line():
            return [l for l in hooks.machine_summary("octo-org/octo-repo", recortex.load_config(), []) if "live dashboard" in l][0]

        self.assertEqual("  live dashboard: not running (starts with the next session)", line())
        self.make_state()
        self.assertEqual("  live dashboard: running on this machine", line())
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "1 2 3 4"}):   # reach() changes no state: it starts everywhere
            self.assertEqual("  live dashboard: running on this machine", line())
        with mock.patch.dict(os.environ, {"RECORTEX_DASHBOARD": "off"}):
            self.assertIn("switched off", line())

    def test_a_server_that_is_there_but_does_not_answer_is_said_so_by_the_doctor(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        self.addCleanup(listener.close)
        dashboard_server.write_state({"form": 1, "pid": os.getpid(), "port": listener.getsockname()[1], "mount": MOUNT,
                                      "view": VIEW, "change": CHANGE, "version": dashboard_server.version(),
                                      "started": int(time.time())})
        line = [l for l in hooks.machine_summary("octo-org/octo-repo", recortex.load_config(), []) if "live dashboard" in l][0]
        self.assertEqual("  live dashboard: running, but it did not answer within 2 seconds (another process may be holding it "
                         "open)", line)

    def test_the_doctors_probe_registers_nothing_and_does_not_extend_the_day(self):
        self.make_state()
        before = self.server.last_ping
        self.server.projects.clear()
        time.sleep(0.05)
        state = {"port": self.server.server_port, "mount": MOUNT}
        self.assertEqual(("running", dashboard_server.version()), dashboard_server.ping(state, self.project, 2.0, probe=True))
        self.assertEqual(before, self.server.last_ping)
        self.assertEqual({}, self.server.projects)
        self.assertEqual(("running", dashboard_server.version()), dashboard_server.ping(state, self.project, 2.0))   # a ping does
        self.assertGreater(self.server.last_ping, before)
        self.assertIn(self.ident, self.server.projects)

    def test_reach_for_each_environment(self):
        self.assertEqual("local", dashboard_server.reach({}))
        for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            self.assertEqual("none", dashboard_server.reach({name: "x"}), name)
            self.assertEqual("editor", dashboard_server.reach({name: "x", "TERM_PROGRAM": "vscode"}), name)
            self.assertEqual("editor", dashboard_server.reach({name: "x", "VSCODE_IPC_HOOK_CLI": "/tmp/s"}), name)
        self.assertEqual("none", dashboard_server.reach({"SSH_CONNECTION": "x", "TERM_PROGRAM": "iTerm.app"}))
        self.assertEqual("local", dashboard_server.reach({"TERM_PROGRAM": "vscode"}))

    def test_where_the_browser_may_not_reach_the_port_the_server_still_starts_and_the_link_is_shown(self):
        """The owner's answer (QD3): the link is offered everywhere. On a machine reached over SSH the session-start hook starts
        the server and puts the link in systemMessage only; reach() decides nothing but whether a question follows `open`."""
        environment = {"SSH_CONNECTION": "1 2 3 4", "TERM_PROGRAM": "iTerm.app"}
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(dashboard_server, "start", side_effect=lambda: self.make_state()) as start:
            self.assertEqual("none", dashboard_server.reach())
            out = self.run_hook("session-start", {})
            prompt = self.run_hook("prompt-submit", {"prompt": "/recortex:dashboard"})
        start.assert_called()
        payload = json.loads(out)
        self.assertIn("recortex dashboard: http://127.0.0.1:%d/%s/p/%s/#%s" % (self.server.server_port, MOUNT, self.ident, CHANGE),
                      payload["systemMessage"])
        self.assertNotIn(CHANGE, payload["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn(hooks.LIVE_FAILED, out)
        self.assertIn(CHANGE, json.loads(prompt)["systemMessage"])
        with mock.patch.dict(os.environ, environment), mock.patch.object(dashboard_server, "start") as start:
            dashboard_server.write_state(dict(self.make_state(), pid=2 ** 22 + 12345, port=1))   # no server answers: one is started
            self.assertEqual("started", dashboard_server.ensure(self.project))
        start.assert_called_once()

    def test_switched_off_by_the_variable_or_the_setting(self):
        for value in ("0", "off", "no", "false", "OFF"):
            with mock.patch.dict(os.environ, {"RECORTEX_DASHBOARD": value}):
                self.assertFalse(dashboard_server.enabled(), value)
                with mock.patch.object(dashboard_server, "start") as start:
                    self.assertEqual("off", dashboard_server.ensure(self.project))
                start.assert_not_called()
        with mock.patch.dict(os.environ, {"RECORTEX_DASHBOARD": "1"}):
            self.assertTrue(dashboard_server.enabled())
        os.makedirs(os.path.dirname(recortex.settings_file()), exist_ok=True)
        with open(recortex.settings_file(), "w", encoding="utf-8") as handle:
            json.dump({"live_dashboard": False}, handle)
        self.assertFalse(dashboard_server.enabled())

    def test_ensure_returns_within_its_limit_against_a_port_that_accepts_and_never_answers(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        self.addCleanup(listener.close)
        dashboard_server.write_state({"form": 1, "pid": os.getpid(), "port": listener.getsockname()[1], "mount": MOUNT,
                                      "view": VIEW, "change": CHANGE, "version": dashboard_server.version(),
                                      "started": int(time.time())})
        with mock.patch.object(dashboard_server, "start") as start:
            began = time.monotonic()
            answer = dashboard_server.ensure(self.project)
            took = time.monotonic() - began
        self.assertEqual("started", answer)
        self.assertLess(took, 1.0)
        start.assert_called_once()

    def test_the_prompt_ping_is_due_once_in_thirty_minutes_for_each_project(self):
        now = 1_800_000_000
        self.assertTrue(dashboard_server.ping_due(self.project, now))
        self.assertFalse(dashboard_server.ping_due(self.project, now + 1799))
        self.assertTrue(dashboard_server.ping_due(self.project, now + 1800))
        second, _ = self.second_project()
        self.assertTrue(dashboard_server.ping_due(second, now + 5))

    def test_the_start_is_detached_without_the_project_in_its_environment(self):
        with mock.patch.object(dashboard_server.subprocess, "Popen") as popen:
            dashboard_server.start()
        arguments, options = popen.call_args
        self.assertEqual("serve", arguments[0][-1])
        self.assertTrue(options["start_new_session"])
        self.assertNotIn("CLAUDE_PROJECT_DIR", options["env"])
        self.assertEqual(subprocess.DEVNULL, options["stdout"])
        self.assertEqual(os.path.expanduser("~"), options["cwd"])
        for argument in arguments[0]:
            self.assertNotIn(CHANGE, argument)


class CommandTests(StateBox):
    """dashboard_action.py open, link, status and stop, as a session and as the user's own terminal run them."""

    def run_command(self, *argv, terminal=False, browser=True):
        out = io.StringIO()
        opened = []
        self.make_state()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(dashboard_server, "start"), \
                mock.patch("observer.on_terminal", return_value=terminal), \
                mock.patch.object(recortex, "open_page", side_effect=lambda url: opened.append(url) or browser):
            code = action.main(list(argv))
        return code, out.getvalue(), opened

    def address(self, key):
        return "http://127.0.0.1:%d/%s/p/%s/#%s" % (self.server.server_port, MOUNT, self.ident, key)

    def test_a_session_gets_the_view_link_from_link_and_never_the_change_key(self):
        code, out, _ = self.run_command("link")
        self.assertEqual(0, code)
        self.assertEqual("live dashboard, view only: %s\n" % self.address(VIEW), out)
        self.assertNotIn(CHANGE, out)

    def test_link_prints_the_view_link_only_even_where_it_sees_a_terminal(self):
        code, out, _ = self.run_command("link", terminal=True)
        self.assertEqual(0, code)
        self.assertEqual("live dashboard, view only: %s\n" % self.address(VIEW), out)
        self.assertNotIn(CHANGE, out)

    def test_open_points_the_browser_at_a_private_launch_file_and_prints_only_the_view_link(self):
        code, out, opened = self.run_command("open")
        self.assertEqual(0, code)
        self.assertEqual(["Opened the live dashboard in your browser.", "live dashboard, view only: " + self.address(VIEW)],
                         out.strip().splitlines())
        self.assertNotIn(CHANGE, out)
        self.assertEqual(1, len(opened))
        self.assertTrue(opened[0].startswith("file://"))
        path = opened[0][len("file://"):]
        self.assertRegex(os.path.basename(path), r"launch-[0-9a-f]{8}\.html")
        self.assertNotIn(path, out)
        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
        with open(path, encoding="utf-8") as handle:
            self.assertIn(self.address(CHANGE), handle.read())

    def test_open_with_a_page_asks_for_that_page_and_adds_no_key_to_the_output(self):
        code, out, opened = self.run_command("open", "--suggestions")
        with open(opened[0][len("file://"):], encoding="utf-8") as handle:
            self.assertIn("/p/%s/suggestions#%s" % (self.ident, CHANGE), handle.read())
        code, out, opened = self.run_command("open", "--impacts")
        with open(opened[0][len("file://"):], encoding="utf-8") as handle:
            self.assertIn("/p/%s/impact-review#%s" % (self.ident, CHANGE), handle.read())
        self.assertNotIn(CHANGE, out)

    def test_when_no_browser_opens_the_reply_names_the_line_above_and_holds_no_change_key(self):
        code, out, opened = self.run_command("open", browser=False)
        self.assertEqual(0, code)
        self.assertIn("live dashboard, view only: " + self.address(VIEW), out)
        self.assertIn("Your private link with changes on is in the line recortex showed above this reply.", out)
        self.assertNotIn(CHANGE, out)
        self.assertIn("Show the file's path", out)   # no browser here: the link first, then Part B's one question

    def test_in_an_editor_no_browser_is_tried(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "1 2 3 4", "TERM_PROGRAM": "vscode"}):
            code, out, opened = self.run_command("open")
        self.assertEqual((0, []), (code, opened))
        self.assertIn("view only", out)
        self.assertNotIn(CHANGE, out)
        self.assertNotIn("Show the file's path", out)   # an editor forwards the port it sees: no question

    def test_where_nothing_shows_the_browser_reaches_the_port_open_gives_the_link_then_the_question(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "1 2 3 4", "TERM_PROGRAM": "iTerm.app"}):
            code, out, opened = self.run_command("open")
        self.assertEqual((0, []), (code, opened))
        lines = out.splitlines()
        self.assertEqual("live dashboard, view only: " + self.address(VIEW), lines[0])
        self.assertEqual("Your private link with changes on is in the line recortex showed above this reply.", lines[1])
        self.assertEqual(action.MAYBE_NOT_REACHED, lines[2])
        self.assertIn("Show the file's path", out)   # Part B's one question follows the link
        self.assertNotIn(CHANGE, out)
        self.assertNotIn("ssh -L", out)
        self.assertNotIn("forward", out)

    def test_open_says_so_when_the_user_switched_the_dashboard_off(self):
        with mock.patch.dict(os.environ, {"RECORTEX_DASHBOARD": "off"}):
            code, out, opened = self.run_command("open")
        self.assertEqual((3, []), (code, opened))
        self.assertEqual(action.SWITCHED_OFF + "\n", out)

    def test_status_says_running_and_stop_ends_the_server_and_says_so(self):
        code, out, _ = self.run_command("status")
        self.assertEqual(0, code)
        self.assertIn("The live dashboard is running on this machine, on port %d" % self.server.server_port, out)
        self.assertNotIn(CHANGE, out)
        code, out, _ = self.run_command("stop")
        self.assertEqual("The live dashboard was stopped.\n", out)
        self.assertTrue(self.server.stop_requested)

    def test_old_launch_files_are_removed_after_a_minute(self):
        folder = action.launch_folder()
        old = os.path.join(folder, "launch-00000000.html")
        new = os.path.join(folder, "launch-11111111.html")
        for path in (old, new):
            storage.write_file(path, "x")
        os.utime(old, (time.time() - 120, time.time() - 120))
        action.prune_launch_files(folder)
        self.assertFalse(os.path.lexists(old))
        self.assertTrue(os.path.lexists(new))


class SessionOutputTests(LiveBox):
    """The real commands, as a session runs them: a server as a subprocess, then `link`, `open` and `status` as subprocesses with
    CLAUDECODE set, no terminal, and a BROWSER that prints the file it is given. Nothing they print holds the change key."""

    def test_what_a_session_sees_from_link_open_and_status_holds_no_change_key(self):
        script = os.path.join(ts.SCRIPTS, "dashboard_action.py")
        child = subprocess.Popen([sys.executable, script, "serve", "--port", "0"], env=dict(os.environ), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline, state = time.time() + 15, None
        while state is None and time.time() < deadline:
            state = dashboard_server.read_state()
            time.sleep(0.05)
        self.assertIsNotNone(state)
        browser = os.path.join(self.dir, "browser.sh")
        with open(browser, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\ncat "${1#file://}"\n')   # a BROWSER that prints the page it was asked to open
        os.chmod(browser, 0o755)
        env = dict(os.environ, CLAUDECODE="1", BROWSER=browser)
        for command in ("link", "open", "status", "open --suggestions"):
            done = subprocess.run([sys.executable, script] + command.split(), env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=120, cwd=self.project)
            seen = done.stdout + done.stderr
            with self.subTest(command=command):
                self.assertEqual(0, done.returncode, seen)
                if command != "status":
                    self.assertIn("live dashboard, view only: ", seen)
                self.assertNotIn(state["change"], seen)
                self.assertNotIn("recortex dashboard:", seen)
        launch = os.listdir(action.launch_folder(create=False))
        self.assertTrue([name for name in launch if action.LAUNCH.fullmatch(name)])   # the browser had a file that holds the key
        dashboard_server.ping(state, self.project, 3.0, stop=True)


class HttpTests(LiveBox):
    """What the 1.7.1 tests of the boundary held, kept for the server of 1.8.0: bounded JSON with an exact schema, the same-origin
    rules of every change, the Stop control and the first snapshot without an index."""

    def test_bounded_json_exact_operation_schema_and_no_paths_or_shell(self):
        before = self.comments()
        for path, data, extra in (("/api/preview", {"step": STEP, "words": "done", "repo": "evil/repo"}, {}),
                                  ("/api/preview", None, {"Content-Length": "999999"}),
                                  ("/api/preview", None, {"Transfer-Encoding": "chunked"}),
                                  ("/../../settings.json", {}, {}), ("/api/shell", {"cmd": "pwd"}, {})):
            status, _, text = self.call("POST", path, data if data is not None else {"step": STEP, "words": "done"}, extra=extra)
            self.assertIn(status, (400, 404), (path, text))
        self.assertEqual(before, self.comments())
        self.assertEqual(200, self.call("POST", "/api/preview", {"step": STEP, "words": WORDS})[0])

    def test_suggestion_feedback_needs_the_change_key_the_same_origin_and_exact_types(self):
        path, data = "/api/suggestions/feedback", {"id": "1" * 64, "muted": True}
        self.assertEqual(403, self.call("POST", path, data, level="view")[0])
        self.assertEqual(403, self.call("POST", path, data, level=None)[0])
        self.assertEqual(403, self.call("POST", path, data, extra={"Origin": "http://foreign.invalid"})[0])
        self.assertEqual(400, self.call("POST", path, dict(data, muted=1))[0])
        self.assertEqual(403, self.call("GET", "/api/suggestions", level=None)[0])
        self.assertEqual(200, self.call("GET", "/api/suggestions")[0])

    def test_stop_needs_the_checks_of_every_post_and_a_refused_request_never_keeps_the_service_alive(self):
        alive = self.server.last_ping
        for extra, level in (({"Host": "attacker.invalid:1234"}, "change"), ({"Origin": "https://attacker.invalid"}, "change"),
                             ({"Sec-Fetch-Site": "cross-site"}, "change"), ({"Content-Type": "text/plain"}, "change"),
                             ({}, "view"), ({}, None)):
            status, _, _ = self.call("POST", "/api/stop", {}, level=level, extra=extra)
            self.assertEqual(403, status, (extra, level))
            self.assertFalse(self.server.stop_requested, (extra, level))
            self.assertEqual(alive, self.server.last_ping)
        self.assertEqual(400, self.call("POST", "/api/stop", {"after": "five"})[0])
        self.assertFalse(self.server.stop_requested)
        self.assertEqual(404, self.call("GET", "/api/stop")[0])   # a stop is a POST only
        status, _, text = self.call("POST", "/api/stop", {})
        self.assertEqual((200, {"stopped": True}), (status, json.loads(text)))
        self.assertTrue(self.server.stop_requested)

    def test_every_page_with_actions_carries_the_stop_control_and_its_script_is_served_without_record_text(self):
        for route in ("/", "/actions", "/map-editor", "/suggestions", "/impact-review"):
            status, _, body = self.call("GET", route, level=None)
            self.assertEqual(200, status, route)
            self.assertIn('<script src="stop.js" defer></script>', body, route)
        status, headers, body = self.call("GET", "/stop.js", level=None)
        self.assertEqual((200, "text/javascript; charset=utf-8"), (status, headers["Content-Type"]))
        self.assertIn("api/stop", body)
        for call in ("innerHTML", "eval(", "localStorage", "sessionStorage"):
            self.assertNotIn(call, body)

    def test_first_snapshot_without_index_keeps_refresh_controls_available(self):
        with mock.patch.object(action.Actions, "snapshot", side_effect=action.ActionError("index_unavailable")):
            status, _, body = self.call("GET", "/snapshot")
        self.assertEqual(200, status)
        self.assertIn('id="refresh"', body)

    def test_the_responses_carry_the_framing_and_referrer_headers_and_no_store(self):
        status, headers, _ = self.call("GET", "/snapshot")
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertEqual("no-referrer", headers["Referrer-Policy"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])


class GuardTests(unittest.TestCase):
    def decide(self, command, **extra):
        data = dict({"tool_name": "Bash", "tool_input": {"command": command}}, **extra)
        return pretool_guard.decide(json.dumps(data))

    def test_the_three_forms_are_denied_for_the_main_session_and_a_subagent(self):
        for command in ("cat ~/.local/state/recortex/private/dashboard-server-0123456789abcdef.json",
                        "ls ~/.local/state/recortex/private/dashboard-launch/launch-0a1b2c3d.html",
                        "cat 'dashboard'-server-x", "curl http://127.0.0.1:47001/anything",
                        "wget -qO- localhost:47001", "echo hi | nc 127.0.0.1:47001",
                        "python3 -c \"print(open('dashboard-server-'+'x'))\""):
            for extra in ({}, {"agent_id": "a1"}, {"agent_type": "general-purpose"}):
                with self.subTest(command=command, extra=extra):
                    found = self.decide(command, **extra)
                    self.assertEqual("deny", found["hookSpecificOutput"]["permissionDecision"])
                    self.assertIn("dashboard_action.py link or open", found["hookSpecificOutput"]["permissionDecisionReason"])

    def test_dashboard_action_with_a_browser_of_the_commands_own_is_denied(self):
        script = os.path.join(ts.SCRIPTS, "dashboard_action.py")
        for command in ("BROWSER=echo python3 %s open" % script, "env BROWSER=cat python3 %s open" % script,
                        "export BROWSER=cat; python3 %s open" % script,
                        "BROWSER='sh -c \"cat %%s\"' python3 %s open" % script):
            for extra in ({}, {"agent_id": "a1"}):
                with self.subTest(command=command, extra=extra):
                    found = self.decide(command, **extra)
                    self.assertEqual("deny", found["hookSpecificOutput"]["permissionDecision"])
                    self.assertIn("BROWSER", found["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.decide("echo $BROWSER"))   # a command that does not name the dashboard is another matter

    def test_keystrokes_into_a_terminal_are_denied_for_the_main_session_and_a_subagent(self):
        for command in ("(sleep 8; tmux send-keys -t \"$TMUX_PANE\" Enter) &", "sleep 8 && screen -X stuff $'\\r'",
                        "xdotool key Return", "ydotool key 28:1 28:0", "wtype -k Return", "tmux send Enter",
                        "tmux paste-buffer -t 0", "screen -S work -X stuff x", "tmux  send-keys Enter"):
            for extra in ({}, {"agent_id": "a1"}):
                with self.subTest(command=command, extra=extra):
                    found = self.decide(command, **extra)
                    self.assertEqual("deny", found["hookSpecificOutput"]["permissionDecision"])
                    self.assertIn("keystrokes", found["hookSpecificOutput"]["permissionDecisionReason"])
        for command in ("tmux ls", "tmux new -s work", "screen -ls", "screen", "echo screen resolution"):
            self.assertIsNone(self.decide(command), command)

    def test_a_hook_is_read_only_only_as_exactly_doctor_or_print_on_its_own(self):
        for command in ("python3 /x/hooks.py prompt-submit < x.json; python3 /x/hooks.py --doctor",
                        "python3 /x/hooks.py --doctor && python3 /x/hooks.py prompt-submit", "python3 /x/hooks.py --doctor x",
                        "python3 /x/hooks.py --print prompt-submit", "bash -c 'python3 /x/hooks.py --doctor'",
                        "/x/run_hook.sh prompt-submit", "python3 /x/hooks.py", "env FOO=1 python3 /x/hooks.py --doctor",
                        "python3 /x/hooks.py --doctor /x/run_hook.sh"):
            with self.subTest(command=command):
                found = self.decide(command)
                self.assertEqual("deny", found["hookSpecificOutput"]["permissionDecision"])
        for command in ("python3 /x/hooks.py --doctor", "python3 /x/hooks.py --print", "/x/hooks.py --doctor",
                        "python3 /x/hooks.py --doctor; ls", "ls && python3.14 /x/hooks.py --print"):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_a_subagents_publish_passes_only_with_its_own_dry_run_or_list(self):
        base = "python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages"
        for command in (base, base + "; echo --dry-run", base + " && ls --list", "bash -c '%s --dry-run'" % base,
                        "echo --dry-run; " + base):
            with self.subTest(command=command):
                found = self.decide(command, agent_id="a1")
                self.assertEqual("deny", found["hookSpecificOutput"]["permissionDecision"])
        for command in (base + " --dry-run", base + " --list", "python3 /x/publish.py --list"):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command, agent_id="a1"))
        self.assertIsNone(self.decide(base))   # the main session's act refuses by itself, without a grant

    def test_the_patterns_are_linear_and_a_long_command_that_names_something_protected_is_denied(self):
        cases = (("python3 /x/observer.py " * 900, {"agent_id": "a1"}), ("dashboard_action.py " * 1000, {}), ("claude " * 3000, {}),
                 ("python3 /x/setup.py " * 1000, {"agent_id": "a1"}), ("python3 /x/hooks.py --doctor; " * 700, {}))
        for command, extra in cases:
            self.assertLess(len(command), pretool_guard.CAP)
            began = time.monotonic()
            self.decide(command, **extra)
            self.assertLess(time.monotonic() - began, 0.5, command[:30])
        padded = "python3 /x/hooks.py prompt-submit < f.json # " + "x" * 100000
        found = self.decide(padded)
        self.assertEqual("deny", found["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual(pretool_guard.TOO_LONG, found["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.decide("echo " + "x" * 100000))   # long, and names nothing protected
        shell = os.path.join(ts.SCRIPTS, "run_pretool_guard.sh")
        data = json.dumps({"tool_name": "Bash", "tool_input": {"command": padded}})
        self.assertIn("deny", subprocess.run(["bash", shell], input=data, capture_output=True, text=True, timeout=30).stdout)

    def test_out_of_time_the_guard_denies_for_a_protected_name_and_for_nothing_else(self):
        def run(command):
            raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(raw)), mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(pretool_guard, "decide", side_effect=TimeoutError("slow")):
                self.assertEqual(0, pretool_guard.main())
            return out.getvalue()

        self.assertEqual(pretool_guard.TOO_LONG, json.loads(run("python3 /x/hooks.py prompt-submit"))["hookSpecificOutput"]
                         ["permissionDecisionReason"])
        self.assertEqual("", run("ls -l"))

    def test_text_after_a_hash_and_names_the_split_does_not_account_for_leave_no_exemption(self):
        publish = "python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages --dry-run"
        for command, extra in (("python3 /x/hooks.py --doctor # python3 /x/hooks.py prompt-submit", {}),
                               ("python3 /x/hooks.py --doctor #python3 /x/run_hook.sh x", {}),
                               (publish + " # python3 /x/publish.py --to a/b --branch main", {"agent_id": "a1"})):
            with self.subTest(command=command):
                self.assertEqual("deny", self.decide(command, **extra)["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.decide("python3 /x/hooks.py --doctor # looks fine"))
        self.assertIsNone(self.decide(publish + " # a dry run", agent_id="a1"))
        pattern = pretool_guard.HOOK_RUN
        self.assertFalse(pretool_guard.counts_agree("python3 /x/hooks.py --doctor", [], pattern))   # the split found none
        self.assertTrue(pretool_guard.counts_agree("python3 /x/hooks.py --doctor", [["python3", "/x/hooks.py", "--doctor"]], pattern))

    def test_a_line_break_leaves_no_exemption(self):
        publish = "python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages --dry-run"
        for command, extra in (("python3 /x/hooks.py --doctor\npython3 /x/hooks.py prompt-submit", {}),
                               ("python3 /x/hooks.py --doctor\rpython3 /x/hooks.py prompt-submit", {}),
                               (publish + "\npython3 /x/publish.py --to a/b --branch main", {"agent_id": "a1"}),
                               (publish + "\r\npython3 /x/publish.py --to a/b --branch main", {"agent_id": "a1"}),
                               # the flag on a line of its own is another command's word, not this one's
                               ("python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages\necho --dry-run", {"agent_id": "a1"}),
                               ("python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages\r\necho --list", {"agent_id": "a1"})):
            with self.subTest(command=command):
                self.assertEqual("deny", self.decide(command, **extra)["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.decide("python3 /x/hooks.py --doctor\n"))   # a closing line break is only the end of the line
        self.assertIsNone(self.decide(publish + "\n", agent_id="a1"))

    def test_the_publish_exemption_needs_the_plain_shape_of_the_hook_one(self):
        flag = " --to octo-org/octo-pages --branch gh-pages --dry-run"
        for command in ("python3 '/tmp/a b/publish.py'" + flag, 'python3 "$HOME/publish.py"' + flag, "env python3 /x/publish.py" + flag,
                        "bash /x/publish.py" + flag, "python3 -u /x/publish.py" + flag, "python3 /x/publish.py /x/publish.py" + flag):
            with self.subTest(command=command):
                self.assertEqual("deny", self.decide(command, agent_id="a1")["hookSpecificOutput"]["permissionDecision"])
        for command in ("python3 /x/publish.py" + flag, "/x/publish.py" + flag, "python3.14 /x/publish.py --list"):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command, agent_id="a1"))

    def test_tmux_and_screen_keys_match_by_prefix_and_the_pipe_that_types(self):
        for command in ("tmux send-k Enter", "tmux send-prefix", "tmux paste -t 0", "tmux paste-buffer", "tmux pasteb",
                        "tmux pipe-pane -I -t 0 'cat /tmp/x'", "tmux pipe-pane -t 0 -IO 'cat /tmp/x'", "screen -S s -X stuff x",
                        "screen -dX stuff x", "screen -rX stuff x"):
            with self.subTest(command=command):
                found = self.decide(command)
                self.assertEqual("deny", found["hookSpecificOutput"]["permissionDecision"])
        for command in ("tmux pipe-pane -t 0 'cat >> /tmp/log'", "tmux ls", "tmux set-option status off", "screen -ls", "screen -dmS work"):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_out_of_time_the_loopback_forms_are_protected_and_the_prefilter_words_are_covered(self):
        def run(command):
            raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(raw)), mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(pretool_guard, "decide", side_effect=TimeoutError("slow")):
                pretool_guard.main()
            return out.getvalue()

        for command in ("curl http://127.0.0.1:47001/x", "wget localhost:47001"):
            self.assertIn("deny", run(command), command)
        # every word of the wrapper's prefilter is read by a decision of the guard: the timeout covers the same names
        with open(os.path.join(ts.SCRIPTS, "run_pretool_guard.sh"), encoding="utf-8") as handle:
            text = handle.read()
        case = text[text.index("case \"$plain\" in") + 1:text.index(") ;;")]
        words = [word.strip("*") for word in re.findall(r"\*[^*|\s]+\*", case)]
        broad = {"recortex", "observer", "hook"}   # prefilter words that only start Python: the guard's own names are the specific ones
        for word in words:
            if word not in broad:
                self.assertRegex(word, pretool_guard.PROTECTED, word)

    def test_dashboard_action_runs_with_no_environment_of_the_commands_own(self):
        script = os.path.join(ts.SCRIPTS, "dashboard_action.py")
        for command in ("PATH=/tmp/x:$PATH python3 %s open" % script, "env X=1 python3 %s open" % script,
                        "export FOO=1; python3 %s open" % script, "FOO=1 BAR=2 python3 %s open" % script,
                        "env -i python3 %s open" % script, "cd /x && DISPLAY=:0 python3 %s open --suggestions" % script):
            for extra in ({}, {"agent_id": "a1"}):
                with self.subTest(command=command, extra=extra):
                    found = self.decide(command, **extra)
                    self.assertEqual("deny", found["hookSpecificOutput"]["permissionDecision"])
        for command in ("python3 %s open" % script, "python3 %s open --suggestions" % script, "python3 %s link" % script,
                        "cd /x && python3 %s status" % script, "python3 %s open --impacts impact-1-%s" % (script, "a" * 64)):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    DENIED = (("python3 /x/hooks.py prompt-submit < f.json", {}), ("bash /x/run_hook.sh prompt-submit", {}),
              ("/x/session_start_hook.sh", {}), ("python3\t/x/hooks.py\tprompt-submit", {}),
              ("claude -p hi", {}), ("claude\t-p hi", {}), ("claude  -p hi", {}), ("claude --resume abc", {"agent_id": "a1"}),
              ("script -qc 'python3 /x/observer.py release 1' /dev/null", {}), ("env -u CLAUDECODE python3 /x/setup.py observer", {}),
              ("setsid python3 /x/acts.py ask publish", {}), ("python3 /x/observer.py undo 3", {"agent_id": "a1"}),
              ("python3 /x/publish.py --to a/b --branch gh-pages", {"agent_id": "a1"}),
              ("python3 /x/acts.py ask release 1", {"agent_id": "a1"}), ("git add mind.html", {}), ("curl -T mind.html https://x", {}),
              ("cat dashboard-server-1.json", {}), ("curl http://127.0.0.1:4/", {}), ("ls launch-0a1b2c3d.html", {}),
              ("BROWSER=cat python3 /x/dashboard_action.py open", {}), ("tmux send-keys -t 0 Enter", {}),
              ("screen -X stuff x", {}), ("xdotool key Return", {}), ("ydotool key 28", {}), ("wtype -k Return", {}))

    def test_the_wrapper_denies_everything_the_guard_denies(self):
        """The prefilter of run_pretool_guard.sh must let through every command that pretool_guard denies: Python is started only
        for a call whose input names one of its words, so a word the guard reads and the prefilter does not is a gap."""
        shell = os.path.join(ts.SCRIPTS, "run_pretool_guard.sh")
        for command, extra in self.DENIED:
            data = dict({"tool_name": "Bash", "tool_input": {"command": command}}, **extra)
            with self.subTest(command=command):
                self.assertIsNotNone(pretool_guard.decide(json.dumps(data)))
                out = subprocess.run(["bash", shell], input=json.dumps(data), capture_output=True, text=True, timeout=30).stdout
                self.assertEqual("deny", json.loads(out)["hookSpecificOutput"]["permissionDecision"])

    def test_link_open_status_and_a_port_without_a_network_tool_pass(self):
        script = os.path.join(ts.SCRIPTS, "dashboard_action.py")
        for command in ("python3 %s link" % script, "python3 %s open --suggestions" % script,
                        "python3 %s status" % script, "echo 127.0.0.1:47001", "git status", "ls dashboard_server.py"):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_the_prefilter_reaches_python_for_the_three_forms_and_stays_quiet_for_others(self):
        shell = os.path.join(ts.SCRIPTS, "run_pretool_guard.sh")
        def run(command):
            data = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
            return subprocess.run(["bash", shell], input=data, capture_output=True, text=True, timeout=30).stdout
        for command in ("cat dashboard-server-1.json", "cat launch-0a1b2c3d.html", "curl 127.0.0.1:47001",
                        "curl 'localhost':1", "cat dashboard\\-server-x.json"):
            with self.subTest(command=command):
                self.assertIn("deny", run(command))
        self.assertEqual("", run("ls -l"))
        self.assertEqual("", run("echo launch"))


if __name__ == "__main__":
    unittest.main()
