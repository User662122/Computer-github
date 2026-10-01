import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".github"))
spec = importlib.util.spec_from_file_location("telegram_runner", REPO / ".github" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)  # must not start polling Telegram at import time


def response(updates):
    result = mock.Mock()
    result.json.return_value = {"ok": True, "result": updates}
    return result


def update(number, text, chat_id=42):
    return {"update_id": number, "message": {"chat": {"id": chat_id}, "text": text}}


class RemoteAccessTests(unittest.TestCase):
    def setUp(self):
        for name, value in {
            "livestream_active": False,
            "livestream_flask_started": True,
            "livestream_server": mock.Mock(),
            "livestream_proc": None,
            "livestream_url": None,
            "livestream_tunnel_thread": None,
            "livestream_stop_event": None,
            "livestream_chat_id": None,
        }.items():
            patch = mock.patch.object(runner, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.send = mock.Mock()
        self.phonefs = mock.Mock()
        self.phonefs.status_text.return_value = "PhoneFS status with dummy credentials"
        for name, value in (("send_message", self.send), ("phonefs_service", self.phonefs)):
            patch = mock.patch.object(runner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_livestream_starts_phonefs_and_reuses_screen_server(self):
        with mock.patch.object(runner, "_start_desktop_tunnel") as start_tunnel:
            runner.livestream(42)
            event = runner.livestream_stop_event
            runner.livestream(42)
        self.assertEqual(runner.livestream_chat_id, 42)
        self.assertTrue(runner.livestream_active)
        self.assertIs(runner.livestream_stop_event, event)
        start_tunnel.assert_called_once_with(42, event)
        self.assertEqual(self.phonefs.start.call_args_list, [mock.call(42), mock.call(42)])

    def test_stop_closes_both_tunnels_without_losing_screen_server(self):
        runner.livestream_chat_id = 42
        runner.livestream_active = True
        runner.livestream_url = "https://screen-test.trycloudflare.com"
        runner.livestream_stop_event = threading.Event()
        process = runner.livestream_proc = mock.Mock()
        thread = runner.livestream_tunnel_thread = mock.Mock()
        server = runner.livestream_server
        with mock.patch.object(runner, "terminate_process_tree") as terminate:
            self.assertTrue(runner.livestream_stop(42))
        self.assertTrue(runner.livestream_stop_event.is_set())
        terminate.assert_called_once_with(process)
        thread.join.assert_called_once_with(timeout=5)
        self.phonefs.stop.assert_called_once_with(42)
        self.assertFalse(runner.livestream_active)
        self.assertIsNone(runner.livestream_proc)
        self.assertIsNone(runner.livestream_url)
        self.assertTrue(runner.livestream_flask_started)
        self.assertIs(runner.livestream_server, server)
        server.shutdown.assert_not_called()

    def test_restart_stops_old_generation_then_starts_both_again(self):
        runner.livestream_chat_id = 42
        runner.livestream_active = True
        old_event = runner.livestream_stop_event = threading.Event()
        with mock.patch.object(runner, "terminate_process_tree"), \
             mock.patch.object(runner, "_start_desktop_tunnel") as start_tunnel:
            runner.livestream_restart(42)
        self.assertTrue(old_event.is_set())
        self.assertIsNot(old_event, runner.livestream_stop_event)
        self.assertFalse(runner.livestream_stop_event.is_set())
        self.phonefs.stop.assert_called_once_with(42)
        self.phonefs.start.assert_called_once_with(42)
        start_tunnel.assert_called_once_with(42, runner.livestream_stop_event)

    def test_another_chat_cannot_view_or_modify_remote_access(self):
        runner.livestream_chat_id = 42
        runner.livestream_url = "https://private-screen.trycloudflare.com"
        runner.livestream_active = True
        runner.livestream(999)
        runner.livestream_status(999)
        runner.livestream_restart(999)
        self.assertFalse(runner.livestream_stop(999))
        self.phonefs.start.assert_not_called()
        self.phonefs.stop.assert_not_called()
        self.phonefs.status_text.assert_not_called()
        self.assertTrue(runner.livestream_active)
        for call in self.send.call_args_list:
            self.assertNotIn(runner.livestream_url, call.args[1])
            self.assertIn("another Telegram chat", call.args[1])

    def test_status_contains_both_services_and_uses_plain_text(self):
        runner.livestream_chat_id = 42
        runner.livestream_url = "https://screen-test.trycloudflare.com"
        with mock.patch.object(runner, "_is_port_open", return_value=True):
            runner.livestream_status(42)
        text = self.send.call_args.args[1]
        self.assertIn(runner.livestream_url, text)
        self.assertIn("PhoneFS status with dummy credentials", text)
        self.assertIsNone(self.send.call_args.kwargs["parse_mode"])

    def test_screen_url_from_merged_stderr_is_announced_with_phonefs_status(self):
        event = runner.livestream_stop_event = threading.Event()
        runner.livestream_chat_id = 42
        process = mock.Mock(pid=12345)
        process.stdout = io.StringIO("\x1b[32m| https://screen-test.trycloudflare.com |\x1b[0m\n")
        process.poll.return_value = None

        def notified(chat_id, text, **kwargs):
            if text.startswith("Screen sharing online"):
                event.set()

        self.send.side_effect = notified
        captured = io.StringIO()
        with mock.patch.object(runner, "_is_port_open", return_value=True), \
             mock.patch.object(runner, "_find_cloudflared", return_value="cloudflared"), \
             mock.patch.object(runner.subprocess, "Popen", return_value=process) as launch, \
             mock.patch.object(runner, "terminate_process_tree"), redirect_stdout(captured):
            runner.run_tunnel_with_autorestart(42, event)
        self.assertEqual(launch.call_args.args[0],
                         ["cloudflared", "tunnel", "--url", "http://127.0.0.1:5000"])
        self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.STDOUT)
        message = self.send.call_args.args[1]
        self.assertIn("https://screen-test.trycloudflare.com", message)
        self.assertIn("PhoneFS status with dummy credentials", message)
        self.assertNotIn("dummy credentials", captured.getvalue())
        self.assertNotIn("https://screen-test.trycloudflare.com", captured.getvalue())

    def test_stale_screen_supervisor_cannot_overwrite_new_session(self):
        old = threading.Event()
        runner.livestream_stop_event = threading.Event()
        current_process = runner.livestream_proc = mock.Mock()
        runner.livestream_url = "https://current-screen.trycloudflare.com"
        with mock.patch.object(runner, "_is_port_open", return_value=True), \
             mock.patch.object(runner.subprocess, "Popen") as launch, \
             mock.patch.object(runner, "terminate_process_tree"):
            runner.run_tunnel_with_autorestart(42, old)
        launch.assert_not_called()
        self.assertIs(runner.livestream_proc, current_process)
        self.assertEqual(runner.livestream_url, "https://current-screen.trycloudflare.com")

    def test_overlapping_browser_stop_and_telegram_start_are_serialized(self):
        runner.livestream_chat_id = 42
        runner.livestream_active = True
        old_event = runner.livestream_stop_event = threading.Event()
        stopping, release, attempting = threading.Event(), threading.Event(), threading.Event()

        def terminate(_):
            stopping.set()
            release.wait(3)

        def start():
            attempting.set()
            runner.livestream(42)

        with mock.patch.object(runner, "terminate_process_tree", side_effect=terminate), \
             mock.patch.object(runner, "_start_desktop_tunnel") as start_tunnel:
            stop_thread = threading.Thread(target=runner.livestream_stop,
                                           kwargs={"notify": False}, daemon=True)
            stop_thread.start()
            self.assertTrue(stopping.wait(2))
            start_thread = threading.Thread(target=start, daemon=True)
            start_thread.start()
            self.assertTrue(attempting.wait(2))
            self.phonefs.start.assert_not_called()
            release.set()
            stop_thread.join(timeout=2)
            start_thread.join(timeout=2)
            self.assertFalse(stop_thread.is_alive())
            self.assertFalse(start_thread.is_alive())
        self.assertTrue(old_event.is_set())
        self.assertTrue(runner.livestream_active)
        self.assertIsNot(old_event, runner.livestream_stop_event)
        self.phonefs.stop.assert_called_once_with(None)
        self.phonefs.start.assert_called_once_with(42)
        start_tunnel.assert_called_once_with(42, runner.livestream_stop_event)

    def test_bot_shutdown_closes_web_server_too(self):
        server = runner.livestream_server
        with mock.patch.object(runner, "livestream_stop") as stop:
            runner.shutdown_remote_access()
        stop.assert_called_once_with(notify=False)
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()

    def test_telegram_command_aliases_dispatch_to_combined_lifecycle(self):
        updates = [update(1, "livestream"), update(2, "live"),
                   update(3, "livestream status"), update(4, "livestream restart"),
                   update(5, "stop stream"), update(6, "/stop")]
        with mock.patch.object(runner, "TOKEN", "test-token"), \
             mock.patch.object(runner, "ALLOWED_CHAT_ID", 42), \
             mock.patch.object(runner.requests, "get", side_effect=[response([]), response(updates)]), \
             mock.patch.object(runner, "livestream") as start, \
             mock.patch.object(runner, "livestream_status") as status, \
             mock.patch.object(runner, "livestream_restart") as restart, \
             mock.patch.object(runner, "livestream_stop") as stop, \
             mock.patch.object(runner, "run_command") as command, \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                runner.main()
        self.assertEqual(start.call_args_list, [mock.call(42), mock.call(42)])
        status.assert_called_once_with(42)
        restart.assert_called_once_with(42)
        stop.assert_called_once_with(42)
        command.assert_not_called()

    def test_session_owner_filter_also_blocks_other_chats_file_and_shell_commands(self):
        runner.livestream_chat_id = 42
        updates = [update(1, "livestream status", 999), update(2, "cmd env", 999),
                   {"update_id": 3, "message": {"chat": {"id": 999},
                    "document": {"file_id": "test-file", "file_name": "script.py"}}},
                   update(4, "/stop", 42)]
        with mock.patch.object(runner, "TOKEN", "test-token"), \
             mock.patch.object(runner, "ALLOWED_CHAT_ID", None), \
             mock.patch.object(runner.requests, "get", side_effect=[response([]), response(updates)]), \
             mock.patch.object(runner, "livestream_status") as status, \
             mock.patch.object(runner, "run_command") as command, \
             mock.patch.object(runner, "download_file") as download, \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                runner.main()
        status.assert_not_called()
        command.assert_not_called()
        download.assert_not_called()


class TelegramMessageTests(unittest.TestCase):
    def test_credentials_are_not_markdown_formatted_or_sent_for_link_preview(self):
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {"ok": True}
        with mock.patch.object(runner.requests, "post", return_value=reply) as send:
            runner.send_message(42, "Generated password: Aa1-example_with-underscores", parse_mode=None)
        payload = send.call_args.kwargs["json"]
        self.assertNotIn("parse_mode", payload)
        self.assertTrue(payload["disable_web_page_preview"])
        self.assertIn("example_with-underscores", payload["text"])

    def test_plain_text_fallback_still_disables_link_preview(self):
        reply = mock.Mock(status_code=400)
        reply.json.return_value = {"ok": False}
        with mock.patch.object(runner.requests, "post", return_value=reply) as send:
            runner.send_message(42, "test_message")
        fallback = send.call_args_list[-1].kwargs["json"]
        self.assertNotIn("parse_mode", fallback)
        self.assertTrue(fallback["disable_web_page_preview"])


if __name__ == "__main__":
    unittest.main()
