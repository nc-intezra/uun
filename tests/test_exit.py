"""Quitting must not hang on background checks (v0.1.0 report on Ubuntu 24.04)."""
import asyncio
import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

from uun import safety

REPO = Path(__file__).resolve().parent.parent


class BackgroundProcesses(unittest.TestCase):
    def tearDown(self):
        safety.SHUTDOWN.clear()

    def test_stop_background_kills_whole_process_group(self):
        import threading
        result = {}
        # parent shell spawns a grandchild that would outlive a plain kill
        t = threading.Thread(target=lambda: result.update(r=safety.run_tracked(
            ["/bin/sh", "-c", "sleep 300 & wait"], timeout=300, env=dict(os.environ))))
        t.start()
        for _ in range(50):
            if safety._children:
                break
            time.sleep(0.05)
        pgid = next(iter(safety._children)).pid
        start = time.monotonic()
        self.assertEqual(safety.stop_background(grace=2), 1)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(result["r"][0], 130)
        time.sleep(0.2)
        ps = subprocess.run(["ps", "-o", "stat=", "-g", str(pgid)], capture_output=True, text=True)
        live = [st for st in ps.stdout.split() if not st.startswith("Z")]  # zombies are dead
        self.assertEqual(live, [], "grandchild survived")

    def test_no_new_checks_after_shutdown(self):
        safety.SHUTDOWN.set()
        self.assertEqual(safety.run_tracked(["/bin/true"], timeout=5, env={})[0], 130)

    def test_timeout_kills_group(self):
        rc, _, err = safety.run_tracked(["/bin/sh", "-c", "sleep 30 & wait"], timeout=0.5, env={})
        self.assertEqual(rc, 124)
        self.assertIn("timed out", err)


class QuitWhileScanning(unittest.TestCase):
    """End to end in a subprocess: a scan stuck on a slow check and on a
    stalled network read must not delay process exit after 'q'."""

    SCRIPT = textwrap.dedent("""
        import asyncio, sys, threading
        sys.path.insert(0, %r)
        from unittest import mock
        from uun import managers, eol
        from uun import __main__ as cli
        from uun.app import UunApp
        slow = managers.Manager("slow", "sleep", "apt",
            lambda: (managers.run(["sleep", "300"], timeout=600), managers.CheckResult())[1], False)
        def run_then_quit(self, *a, **k):
            async def go():
                async with self.run_test(size=(120, 40)) as pilot:
                    await pilot.pause(1.5)
                    self.updating = True
                    await pilot.press("q"); await pilot.pause(0.2)
                    print("REFUSED" if self.is_running else "QUIT_DURING_UPDATE", flush=True)
                    self.updating = False
                    await pilot.press("q")
            asyncio.run(go())
        with mock.patch.object(managers, "available", lambda cfg: [slow]), \\
             mock.patch.object(eol, "cache_age_hours", lambda *a: None), \\
             mock.patch.object(eol, "refresh", lambda *a, **k: threading.Event().wait(600)), \\
             mock.patch.object(UunApp, "run", run_then_quit), \\
             mock.patch.object(sys.stdout, "isatty", lambda: True):
            cli.main([])
    """) % str(REPO)

    def test_quit_exits_promptly_and_refuses_during_update(self):
        start = time.monotonic()
        r = subprocess.run([sys.executable, "-c", self.SCRIPT], capture_output=True, text=True,
                           timeout=60)
        elapsed = time.monotonic() - start
        self.assertIn("REFUSED", r.stdout, r.stderr[-2000:])
        self.assertLess(elapsed, 15, f"exit took {elapsed:.1f}s")
        time.sleep(0.2)
        ps = subprocess.run(["ps", "-eo", "stat=,args="], capture_output=True, text=True).stdout
        live = [ln for ln in ps.splitlines() if ln.split(None, 1)[-1] == "sleep 300"
                and not ln.lstrip().startswith("Z")]
        self.assertEqual(live, [], "check process left running after quit")



class ShutdownRace(unittest.TestCase):
    """The app's own timers firing during teardown must not crash (CI armhf
    failure: NoMatches '#clock' from the 1 s clock tick). The app's timers are
    made to tick every 1 ms to make the race window large and deterministic."""

    def _run(self, quit_with_q):
        from unittest import mock

        from textual.app import App

        from uun.app import UunApp
        from uun.config import load_config

        def fast_interval(self, interval, callback, *a, **k):
            return App.set_interval(self, 0.001, callback, *a, **k)

        async def once():
            app = UunApp(load_config(), demo=True)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(0.3)
                if quit_with_q:
                    # direct call: pilot.press() waits for idle, which a 1 ms
                    # clock never allows
                    await app.action_quit()
        with mock.patch.object(UunApp, "set_interval", fast_interval):
            for _ in range(5):
                start = time.monotonic()
                asyncio.run(asyncio.wait_for(once(), 30))
                self.assertLess(time.monotonic() - start, 15)

    def test_teardown_with_pending_ticks(self):
        self._run(quit_with_q=False)

    def test_quit_with_pending_ticks(self):
        try:
            self._run(quit_with_q=True)
        finally:
            safety.SHUTDOWN.clear()

if __name__ == "__main__":
    unittest.main()
