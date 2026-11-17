"""Instant prompts over Gemini Live. Run with: python3 -m unittest tests.test_live

The Live connection and the phone's WebSocket are replaced by scripted fakes; everything else, including the
google-genai types, is real. tests.test_live_app covers the Flask side.
"""
import asyncio
import importlib.util
import json
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import live

HAVE_SDK = bool(importlib.util.find_spec("google.genai")) and bool(importlib.util.find_spec("simple_websocket"))
PREPARATION = {"audience": ["Mother"], "topic": "Studying further", "goals": [{"id": "g1", "description": "Explain why it matters"},
                                                                              {"id": "g2", "description": "Agree on a next step"}],
               "expectations": {"what_might_go_well": [], "possible_barriers": ["Fees"]}}
PLAN = {"understanding": "Explain the course, then agree a next step.", "assumptions": [],
        "steps": [{"id": "step1", "goal_ids": ["g1"], "purpose": "Explain", "talking_points": ["The course"], "example_wording": "This course matters."},
                  {"id": "step2", "goal_ids": ["g2"], "purpose": "Agree", "talking_points": ["A visit"], "example_wording": "Could we visit?"}]}
SESSION = {"preparation": PREPARATION, "chosen_plan": PLAN}
CALL_ARGS = {"threat": False, "audio_status": "clear", "notes": "Fees came up.", "current_step_id": "step1", "raised_goals": [],
             "situation": "unanswered_concern", "situation_evidence": "A worry about fees was not answered.",
             "cue": {"text": "Acknowledge the worry about fees.", "example_wording": "Could I explain why this matters to me?", "step_id": "step1"}}


def wait_for(condition, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class DeclarationTests(unittest.TestCase):
    def test_the_function_carries_exactly_the_regular_prompt_contract(self):
        declaration = live.function_declaration(SESSION)
        self.assertEqual((declaration["name"], declaration["behavior"]), ("assess_moment", "NON_BLOCKING"))
        parameters = declaration["parameters"]
        self.assertEqual(parameters["type"], "OBJECT")
        self.assertEqual(list(parameters["properties"]), ["threat", "audio_status", "notes", "current_step_id", "raised_goals",
                                                          "situation", "situation_evidence", "cue"])
        self.assertEqual(parameters["properties"]["situation"]["enum"], list(live.gemini.SITUATIONS))
        self.assertNotIn("additionalProperties", json.dumps(parameters))
        self.assertNotIn("kind", parameters["properties"]["cue"]["properties"])  # The server still picks the kind.

    def test_the_instructions_are_the_regular_ones_with_the_live_rules_first(self):
        text = live.system_instruction(SESSION, "hi")
        self.assertTrue(text.startswith(live.LIVE_INTRO))
        self.assertIn("respond only by calling", text)
        self.assertIn("call assess_moment at once with threat true", text)
        self.assertIn('"cue_language": "Hindi in Devanagari script"', text)
        self.assertTrue(text.endswith(live.gemini.FACILITATOR_PROMPT))

    def test_a_check_carries_what_the_server_remembers(self):
        text = live.check_text(True, 75.4, "Fees came up.", ["g1"], [{"kind": "next_point", "text": "a"}] * 5)
        self.assertTrue(text.startswith("CHECK ") and text.endswith(" Call assess_moment now."))
        sent = json.loads(text[len("CHECK "):-len(" Call assess_moment now.")])
        self.assertEqual((sent["requested"], sent["elapsed_seconds"]), (True, 75))
        self.assertEqual(len(sent["conversation_so_far"]["recent_prompts"]), 3)

    def test_instant_prompts_need_the_libraries_a_key_and_no_switch_off(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertFalse(live.enabled())
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k", "LIVE_PROMPTS": "0"}):
            self.assertFalse(live.enabled())
        if HAVE_SDK and importlib.util.find_spec("flask_sock"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k", "LIVE_PROMPTS": ""}):
                self.assertTrue(live.enabled())


class FakeLiveSession:
    """A scripted Gemini Live connection."""
    def __init__(self):
        self.sent, self.incoming, self.ended = [], None, False
        self.loop = None

    async def send_realtime_input(self, **kwargs):
        self.sent.append(("audio", kwargs["audio"]))

    async def send_client_content(self, **kwargs):
        self.sent.append(("check", kwargs))

    async def send_tool_response(self, **kwargs):
        self.sent.append(("answer", kwargs["function_responses"][0]))

    def push(self, message):
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, message)

    async def receive(self):
        if self.ended:
            return
        while True:
            message = await self.incoming.get()
            if message is None:
                self.ended = True
                return
            yield message


@unittest.skipUnless(HAVE_SDK, "google-genai unavailable")
class RelayTests(unittest.TestCase):
    def setUp(self):
        self.sessions, self.handles = [], []

        relay_test = self

        class Connection:
            def __init__(self, handle):
                relay_test.handles.append(handle)
                self.session = FakeLiveSession()

            async def __aenter__(self):
                self.session.loop = asyncio.get_running_loop()
                self.session.incoming = asyncio.Queue()
                relay_test.sessions.append(self.session)
                return self.session

            async def __aexit__(self, *exc):
                return False
        self.relay = live.Relay(SESSION, "en", connect=Connection)

    def tearDown(self):
        self.relay.close()
        self.relay.thread and self.relay.thread.join(2)

    def events(self, kind):
        found = []
        def check():
            found.extend(event for event in self.relay.drain() if event[0] == kind)
            return bool(found)
        self.assertTrue(wait_for(check), f"no {kind} event")
        return found

    def test_audio_checks_calls_and_silent_answers_flow_through_one_session(self):
        self.relay.start()
        self.events("ready")
        session = self.sessions[0]
        self.relay.audio(b"\x01\x00" * 160)
        self.relay.check("CHECK {} Call assess_moment now.")
        self.assertTrue(wait_for(lambda: len(session.sent) == 2))
        audio, check = session.sent
        self.assertEqual((audio[1].mime_type, audio[1].data), ("audio/pcm;rate=16000", b"\x01\x00" * 160))
        self.assertTrue(check[1]["turn_complete"])
        self.assertEqual(check[1]["turns"].parts[0].text, "CHECK {} Call assess_moment now.")
        call = SimpleNamespace(id="call-1", name="assess_moment", args=CALL_ARGS)
        session.push(SimpleNamespace(tool_call=SimpleNamespace(function_calls=[call]), session_resumption_update=None, go_away=None))
        [(_, payload)] = self.events("call")
        self.assertEqual(payload, {"id": "call-1", "name": "assess_moment", "args": CALL_ARGS})
        self.relay.answer("call-1", "assess_moment")
        self.assertTrue(wait_for(lambda: len(session.sent) == 3))
        answer = session.sent[2][1]
        self.assertEqual((answer.id, answer.name, answer.response), ("call-1", "assess_moment", {"result": "ok"}))
        self.assertEqual(answer.scheduling, live_types().FunctionResponseScheduling.SILENT)

    def test_gemini_transcribes_what_it_hears_for_the_live_view(self):
        self.assertIsNotNone(self.relay._config().input_audio_transcription)
        self.relay.start()
        self.events("ready")
        self.sessions[0].push(SimpleNamespace(tool_call=None, go_away=None, session_resumption_update=None,
                                              server_content=SimpleNamespace(input_transcription=SimpleNamespace(text="The fees", finished=False))))
        [(_, payload)] = self.events("heard")
        self.assertEqual(payload, {"text": "The fees", "finished": False})

    def test_a_connection_gemini_ends_is_resumed_with_the_latest_handle(self):
        self.relay.start()
        self.events("ready")
        first = self.sessions[0]
        first.push(SimpleNamespace(tool_call=None, go_away=None,
                                   session_resumption_update=SimpleNamespace(resumable=True, new_handle="handle-1")))
        first.push(SimpleNamespace(tool_call=None, session_resumption_update=None, go_away=SimpleNamespace(time_left="10s")))
        self.assertTrue(wait_for(lambda: len(self.sessions) == 2))
        self.assertEqual(self.handles, [None, "handle-1"])
        self.relay.audio(b"\x02\x00" * 8)
        self.assertTrue(wait_for(lambda: self.sessions[1].sent))  # Audio continues on the resumed connection.
        self.assertEqual(self.relay.drain(), [])  # Resuming is quiet: no second "ready".

    def test_repeated_connection_failures_stop_instant_prompts_but_say_so(self):
        class Failing:
            def __init__(self, handle):
                pass

            async def __aenter__(self):
                raise ConnectionError("no network")

            async def __aexit__(self, *exc):
                return False
        relay = live.Relay(SESSION, "en", connect=Failing)
        with patch("live.asyncio.sleep", new=_no_sleep):
            relay.start()
            self.assertTrue(wait_for(lambda: not relay.running))
        self.assertEqual([kind for kind, _ in relay.drain()], ["stopped"])


async def _no_sleep(*args, **kwargs):
    return None


def live_types():
    from google.genai import types
    return types


class FakeSocket:
    """The phone's side of the WebSocket: scripted messages in, everything sent recorded."""
    def __init__(self, script):
        self.script, self.sent = list(script), []

    def send(self, text):
        self.sent.append(json.loads(text))

    def receive(self, timeout=None):
        if not self.script:
            from simple_websocket import ConnectionClosed
            raise ConnectionClosed()
        item = self.script.pop(0)
        return item() if callable(item) else item


class FakeRelay:
    def __init__(self):
        self.queue, self.audio_frames, self.checks, self.answers, self.closed = [], [], [], [], False

    running = True

    def start(self):
        self.queue.append(("ready", None))

    def drain(self):
        items, self.queue = self.queue, []
        return items

    def audio(self, pcm):
        self.audio_frames.append(pcm)

    def check(self, text):
        self.checks.append(text)

    def answer(self, call_id, name):
        self.answers.append(call_id)

    def close(self):
        self.closed = True


@unittest.skipUnless(HAVE_SDK, "simple-websocket unavailable")
class ServeTests(unittest.TestCase):
    def run_serve(self, script, on_call, clock=None, instruction=None):
        relay, calls = FakeRelay(), []
        if instruction:
            relay.instruction = instruction

        def recording_call(args, requested, elapsed, mode, unprompted=False):
            calls.append((requested, elapsed, mode))
            return on_call(args)
        ws = FakeSocket([(lambda step=item: step(relay)) if callable(item) else item for item in script])  # Steps run in order.
        live.serve(ws, relay, on_check=lambda requested, elapsed: f"CHECK {requested} {elapsed}", on_call=recording_call,
                   clock=clock or time.monotonic, receive_timeout=0)
        return ws, relay, calls

    def test_a_tap_becomes_a_check_and_the_judgement_comes_back_as_a_text_cue(self):
        guidance = {"prompt": {"kind": "respond_to_concern", "text": "Acknowledge the worry."}}
        ws, relay, calls = self.run_serve([
            b"\x01\x00" * 16000,  # one second of audio
            b"\x01", b"\x00" * (live.MAX_AUDIO_FRAME + 2),  # odd-sized and oversized frames are dropped
            json.dumps({"type": "check", "id": 1, "requested": True, "elapsed_seconds": 12, "mode": "on_request"}),
            lambda relay: relay.queue.append(("call", {"id": "c1", "name": "assess_moment", "args": CALL_ARGS})),
            None,
        ], on_call=lambda args: guidance)
        self.assertEqual(ws.sent[0], {"type": "ready"})
        self.assertIn({"type": "audio", "seconds": 1.0}, ws.sent)  # Her phone hears that audio is arriving...
        self.assertIn({"type": "sent", "id": 1, "check": "CHECK True 12.0"}, ws.sent)  # ...and exactly what was asked.
        self.assertEqual([m for m in ws.sent if m["type"] == "guidance"], [{"type": "guidance", "id": 1, "guidance": guidance}])
        self.assertEqual(calls, [(True, 12.0, "on_request")])
        self.assertEqual((len(relay.audio_frames), relay.checks, relay.answers), (1, ["CHECK True 12.0"], ["c1"]))
        self.assertTrue(relay.closed)

    def test_a_threat_reported_without_a_check_is_shown_at_once(self):
        ws, relay, calls = self.run_serve([
            b"\x01\x00" * 32000,  # two seconds of audio
            lambda relay: relay.queue.append(("call", {"id": "c9", "name": "assess_moment", "args": CALL_ARGS})),
            None,
        ], on_call=lambda args: {"prompt": {"kind": "step_away"}})
        self.assertEqual([m for m in ws.sent if m["type"] == "guidance"],
                         [{"type": "guidance", "id": None, "guidance": {"prompt": {"kind": "step_away"}}}])
        self.assertEqual(calls, [(False, 2.0, "on_request")])  # A background judgement at the streamed time.
        self.assertEqual(relay.answers, ["c9"])

    def test_unprompted_judgements_that_are_not_threats_never_reach_her_phone(self):
        ws, relay, calls = self.run_serve([
            lambda relay: relay.queue.append(("call", {"id": "c5", "name": "assess_moment", "args": CALL_ARGS})),
            None,
        ], on_call=lambda args: {"prompt": {"kind": "respond_to_concern", "text": "x"}})
        self.assertEqual([message["type"] for message in ws.sent], ["ready"])
        self.assertEqual((len(calls), relay.answers), (1, ["c5"]))  # Still saved and answered, silently.

    def test_the_live_rules_ask_for_the_last_minute_not_the_pause(self):
        for rule in ("above all its last minute of speech", "never answer a concern", "Do not call it at any other time"):
            self.assertIn(rule, live.LIVE_INTRO)

    def test_what_gemini_hears_is_shown_live_but_never_saved(self):
        ws, relay, calls = self.run_serve([
            lambda relay: relay.queue.append(("heard", {"text": "The fees are too high.", "finished": True})),
            None,
        ], on_call=lambda args: {})
        self.assertIn({"type": "heard", "text": "The fees are too high.", "finished": True}, ws.sent)
        self.assertEqual(calls, [])  # Nothing is validated or saved for a transcript.

    def test_every_answer_shows_what_went_in_and_what_came_back(self):
        guidance = {"prompt": {"kind": "respond_to_concern", "text": "Acknowledge the worry."}}
        ws, relay, _ = self.run_serve([
            json.dumps({"type": "check", "id": 7, "requested": True, "elapsed_seconds": 5, "mode": "on_request"}),
            lambda relay: relay.queue.append(("call", {"id": "c7", "name": "assess_moment", "args": CALL_ARGS})),
            None,
        ], on_call=lambda args: guidance, instruction="LIVE INSTRUCTIONS")
        self.assertIn({"type": "setup", "instruction": "LIVE INSTRUCTIONS"}, ws.sent)
        self.assertIn({"type": "sent", "id": 7, "check": "CHECK True 5.0"}, ws.sent)
        self.assertIn({"type": "answered", "id": 7, "raw": CALL_ARGS, "shown": guidance["prompt"], "reason": None}, ws.sent)

    def test_invalid_judgements_and_slow_checks_end_quietly(self):
        now = [100.0]

        def invalid(args):
            raise ValueError("bad")
        ws, relay, _ = self.run_serve([
            json.dumps({"type": "check", "id": 2, "requested": True, "elapsed_seconds": 30, "mode": "auto"}),
            lambda relay: relay.queue.append(("call", {"id": "c2", "name": "assess_moment", "args": {}})),
            None,
            json.dumps({"type": "check", "id": 3, "requested": False, "elapsed_seconds": 45, "mode": "auto"}),
            lambda relay: now.__setitem__(0, now[0] + live.CHECK_TIMEOUT_SECONDS + 1),
            None,
            json.dumps({"type": "check", "id": "x", "requested": True, "elapsed_seconds": 1, "mode": "auto"}),  # ignored
            json.dumps({"type": "stop"}),
        ], on_call=invalid, clock=lambda: now[0])
        errors = [message for message in ws.sent if message["type"] == "error"]
        self.assertEqual([(e["id"], e["message"], e.get("reason")) for e in errors], [(2, live.NO_PROMPT, "bad"), (3, live.NO_PROMPT, None)])
        self.assertIn({"type": "answered", "id": 2, "raw": {}, "shown": None, "reason": "bad"}, ws.sent)  # The rejected answer, as given.
        self.assertEqual(relay.answers, ["c2"])  # Even an invalid call is answered, silently.
        self.assertEqual(len(relay.checks), 2)
        self.assertTrue(relay.closed)

    def test_a_stopped_relay_tells_the_phone_and_ends(self):
        ws, relay, _ = self.run_serve([lambda relay: relay.queue.append(("stopped", "Instant prompts stopped.")), None, None],
                                      on_call=lambda args: {})
        self.assertEqual(ws.sent[-1], {"type": "stopped", "message": "Instant prompts stopped."})
        self.assertTrue(relay.closed)


if __name__ == "__main__":
    unittest.main()
