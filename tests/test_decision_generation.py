from __future__ import annotations

import asyncio
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

os.environ.setdefault("SERVICE_TOKEN", "test")

from control_plane.ai_capabilities import CapabilityNotFound
from control_plane.api import DecisionGenerationRequest, _validate_decision_generation_request
from control_plane.decision_generation_jobs import DecisionGenerationJobClient
from control_plane.decision_generation_tasks import _answers, _decide, _payload, decide
from control_plane.observability import route_info
from fastapi import HTTPException

# 2026-09-30 实测的响应体裁出来的（只留解析用得到的字段）。
CHOICE_ANSWER = {
    "model": "typesafe-ai/jev",
    "answers": {
        "risk": {
            "type": "choice",
            "choice": "medium",
            "confidence": 0.72,
            "probabilities": {"low": 0.2, "medium": 0.72, "high": 0.08},
        },
        "urgency": {"type": "noul", "noul": 0.4},
    },
    "usage": {"prompt_tokens": 118, "completion_tokens": 42},
}
BINDING = {
    "adapter": "teamorouter",
    "channel_id": "chan-teamorouter",
    "upstream_model": "typesafe-ai/jev",
    "base_url": "https://api.teamorouter.com",
    "submit_path": "/v1/systemone",
    "query_path": "",
    "timeout_seconds": 60,
    "auth_type": "bearer",
    "credential": "teamorouter-key",
    "enabled": 1,
    "channel_enabled": 1,
    "health_status": "online",
    "capabilities": {"images": 0, "videos": 0, "audios": 0},
}


def decision_request(**overrides):
    base = {
        "model": "jev",
        "channel": "teamorouter",
        "state": "客户在 09-30 提出要改枪皮配色，上周已经改过两轮。",
        "questions": {
            "risk": {"type": "choice", "instructions": "这一轮返工的风险有多高？",
                     "criteria": {"low": "只是微调", "medium": "需要重渲", "high": "方向要重定"}},
        },
        "external_ref": None,
        "metadata": {},
    }
    base.update(overrides)
    return base


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body, self.status_code = body, status_code

    def json(self) -> dict:
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body)


class FakeClient:
    """记下请求过的 URL 与请求体；按预设脚本依次回响应（auto 换渠道时要用到）。"""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses, self.urls, self.payloads = responses, [], []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url: str, headers=None, json=None) -> FakeResponse:
        self.urls.append(url)
        self.payloads.append((headers, json))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class FakeStore:
    def __init__(self, request: dict, channels=("teamorouter",)) -> None:
        self.record = {"id": "job-1", "status": "queued", "request": request}
        self.updates: list[dict] = []
        self.channels = list(channels)

    def job(self, job_id: str, include_request: bool = False) -> dict:
        out = dict(self.record)
        if not include_request:
            out.pop("request", None)
        return out

    def binding_channels(self, model: str) -> list[str]:
        return list(self.channels)

    def binding(self, model: str, code: str) -> dict:
        if code not in self.channels:
            raise CapabilityNotFound(code)
        return {**BINDING, "channel_id": f"chan-{code}", "adapter": code, "credential": f"{code}-key"}

    def update_job(self, job_id: str, **values) -> None:
        self.updates.append(values)
        self.record.update(values)


class PayloadTests(unittest.TestCase):
    def test_payload_sends_the_state_and_the_questions_verbatim(self) -> None:
        request = decision_request()
        payload = _payload(BINDING, request)

        self.assertEqual(payload, {"model": "typesafe-ai/jev", "state": request["state"], "questions": request["questions"]})
        # 题型结构不在本地重编：choice 的 criteria 是对象、score 的是数组，喂错上游报得比我们准。
        self.assertEqual(payload["questions"]["risk"]["criteria"]["medium"], "需要重渲")

    def test_answers_requires_a_non_empty_dict(self) -> None:
        self.assertEqual(_answers(CHOICE_ANSWER), CHOICE_ANSWER["answers"])
        # `answers: {}` 对调用方和「没有答案」是一回事，不能当成成功。
        self.assertIsNone(_answers({"answers": {}}))
        self.assertIsNone(_answers({"answers": []}))
        self.assertIsNone(_answers({}))


class DecideTests(unittest.TestCase):
    def _run(self, responses: list[FakeResponse], **overrides):
        channels = overrides.pop("channels", ("teamorouter",))
        request = decision_request(**overrides)
        store = FakeStore(request, channels=channels)
        client = FakeClient(responses)
        with patch("control_plane.decision_generation_tasks._store", return_value=store), patch(
            "control_plane.decision_generation_tasks.httpx.Client", return_value=client
        ):
            _decide("job-1")
        self.store, self.client = store, client
        return store.record

    def test_a_answered_decision_succeeds_without_a_task_id(self) -> None:
        record = self._run([FakeResponse(CHOICE_ANSWER)])

        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["stage"], "completed")
        # Jev 是同步的：没有 task id、没有媒体成品，答案整个落在 upstream_response 里。
        self.assertEqual(record["upstream_task_id"], "")
        self.assertEqual(json.loads(record["result_urls_json"]), [])
        self.assertEqual(json.loads(record["upstream_response_json"])["answers"]["risk"]["choice"], "medium")
        self.assertIsNone(record["error"])
        self.assertEqual(self.client.urls, ["https://api.teamorouter.com/v1/systemone"])
        headers, payload = self.client.payloads[0]
        self.assertEqual(headers["Authorization"], "Bearer teamorouter-key")
        self.assertEqual(set(payload), {"model", "state", "questions"})

    def test_an_error_body_fails_the_job_with_the_upstream_message(self) -> None:
        body = {"error": {"message": "criteria must contain 2 to 10 levels", "type": "invalid_request_error"}}
        record = self._run([FakeResponse(body)])

        self.assertEqual(record["status"], "failed")
        self.assertIn("criteria must contain 2 to 10 levels", record["error"])
        self.assertNotIn("job-1", record["error"])

    def test_an_http_error_keeps_the_status_code_and_the_message(self) -> None:
        record = self._run([FakeResponse({"error": {"message": "invalid api key"}}, status_code=401)])

        self.assertEqual(record["status"], "failed")
        self.assertIn("HTTP 401", record["error"])
        self.assertIn("invalid api key", record["error"])

    def test_a_response_without_answers_is_a_failure_not_an_empty_success(self) -> None:
        record = self._run([FakeResponse({"model": "typesafe-ai/jev"})])

        self.assertEqual(record["status"], "failed")
        self.assertIn("did not contain answers", record["error"])

    def test_auto_falls_back_to_the_next_channel(self) -> None:
        record = self._run(
            [FakeResponse({"error": {"message": "busy"}}), FakeResponse(CHOICE_ANSWER)],
            channel="auto",
            channels=("grsai", "teamorouter"),
        )

        self.assertEqual(record["status"], "succeeded")
        # 第一次提交被上游拒（auto 的意义就在这里），第二次接住；fallback_count 记的是
        # **候选列表里的位次**（这里是 1），不是失败次数。
        self.assertEqual(len(self.client.urls), 2)
        self.assertEqual(record["fallback_count"], 1)

    def test_stages_are_reported_in_order(self) -> None:
        self._run([FakeResponse(CHOICE_ANSWER)])

        stages = [update.get("stage") for update in self.store.updates]
        self.assertEqual(stages[0], "selecting_channel")
        self.assertIn("submitting", stages)

    def test_a_cancel_wins_over_a_finished_decision_but_keeps_the_answers(self) -> None:
        request = decision_request()
        store = FakeStore(request)

        def update_job(job_id, **values):
            store.updates.append(values)
            store.record.update(values)
            # 撤单落在「已提交、还没收尾」这个窗口里：答案已经拿到了。
            if values.get("stage") == "submitting":
                store.record["status"] = "cancel_requested"

        store.update_job = update_job
        with patch("control_plane.decision_generation_tasks._store", return_value=store), patch(
            "control_plane.decision_generation_tasks.httpx.Client", return_value=FakeClient([FakeResponse(CHOICE_ANSWER)])
        ):
            _decide("job-1")

        self.assertEqual(store.record["status"], "cancelled")
        # 答案不丢：撤单只是不把它标成 succeeded，已经拿到的判定仍然留在记录里。
        self.assertEqual(json.loads(store.record["upstream_response_json"])["answers"]["risk"]["choice"], "medium")

    def test_a_persistence_failure_after_the_answer_does_not_re_buy_it(self) -> None:
        store = FakeStore(decision_request(channel="auto"), channels=("grsai", "teamorouter"))
        client = FakeClient([FakeResponse(CHOICE_ANSWER)])
        original = store.update_job

        def update_job(job_id, **values):
            # 答案已经拿到、写库才炸：这时候换渠道重问等于再买一次，而且定型判定是概率输出，
            # 第二次未必给同一份。所以记完失败就停，不能再问下一家。
            if values.get("upstream_response_json"):
                raise RuntimeError("database is locked")
            original(job_id, **values)

        store.update_job = update_job
        with patch("control_plane.decision_generation_tasks._store", return_value=store), patch(
            "control_plane.decision_generation_tasks.httpx.Client", return_value=client
        ):
            _decide("job-1")

        self.assertEqual(store.record["status"], "failed")
        self.assertEqual(len(client.urls), 1)
        self.assertIn("database is locked", store.record["error"])


@contextmanager
def _no_slot(*_args, **kwargs):
    """替掉并发闸门：闸门自己由 tests/test_concurrency.py 覆盖。

    不替的话 `.apply()` 会真的去连 Redis，单测就变成依赖外部服务了。
    """
    yield {"limit": 4}


class TaskWrapperTests(unittest.TestCase):
    def test_the_gate_is_the_decision_queue_and_it_reports_waiting(self) -> None:
        store = FakeStore(decision_request())
        captured: dict = {}

        def slot(module, **kwargs):
            captured["module"] = module
            captured["on_wait"] = kwargs.get("on_wait")
            return _no_slot()

        with (
            patch("control_plane.decision_generation_tasks.job_slot", slot),
            patch("control_plane.decision_generation_tasks._decide", return_value={"status": "succeeded"}) as decide_call,
            patch("control_plane.decision_generation_tasks._store", return_value=store),
        ):
            eager = decide.apply(kwargs={"job_id": "job-1"}, task_id="job-1", throw=True)
            eager.get(propagate=True)
            self.assertEqual(captured["module"], "decision_generation")
            self.assertEqual(decide_call.call_args.args[0], "job-1")
            # 排队要如实上报：否则卡在闸门上的作业对外仍然是 running —— 这正是当初分不开
            # 「排队」和「卡死」的成因。
            captured["on_wait"](4)

        self.assertEqual(store.record["stage"], "waiting_slot")


class JobClientTests(unittest.TestCase):
    @patch("control_plane.generation_jobs.GenerationJobClient._app")
    def test_submit_lands_on_the_decision_queue(self, app: Mock) -> None:
        store = Mock()
        store.create_job.return_value = "job-1"
        client = DecisionGenerationJobClient(Mock(), store)

        client.submit(decision_request())

        store.create_job.assert_called_once()
        self.assertEqual(store.create_job.call_args.args[0], "jev")
        self.assertEqual(store.create_job.call_args.args[1], "teamorouter")
        app.return_value.send_task.assert_called_once_with(
            "control_plane.decision_generation",
            kwargs={"job_id": "job-1"},
            task_id="job-1",
            queue="decision_generation",
        )


class ObservabilityRouteTests(unittest.TestCase):
    def test_routes_are_observable(self) -> None:
        create = route_info("POST", "/v1/decision-generations/jobs")

        self.assertEqual(create.service, "decision_generation")
        self.assertTrue(create.creates_task)
        self.assertTrue(route_info("GET", "/v1/decision-generations/jobs/2f1c5a44-0000-4000-8000-000000000000").status_query)
        self.assertTrue(route_info("POST", "/v1/decision-generations/jobs/2f1c5a44-0000-4000-8000-000000000000/cancel").cancel)


class ValidationTests(unittest.TestCase):
    def _reject(self, **kwargs) -> str:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(_validate_decision_generation_request(DecisionGenerationRequest(**kwargs)))
        self.assertEqual(caught.exception.status_code, 422)
        return caught.exception.detail

    def test_questions_must_not_be_empty(self) -> None:
        self.assertIn("questions", self._reject(state="x", questions={}))

    def test_each_question_needs_a_type_and_instructions(self) -> None:
        self.assertIn("'risk' requires a type", self._reject(state="x", questions={"risk": {"instructions": "?"}}))
        self.assertIn(
            "'risk' requires instructions",
            self._reject(state="x", questions={"risk": {"type": "choice"}}),
        )

    def test_an_unavailable_channel_is_rejected(self) -> None:
        store = Mock()
        store.binding.return_value = {**BINDING, "health_status": "unknown"}
        with patch("control_plane.api.get_capability_store", return_value=store):
            self.assertIn("no enabled, healthy channel", self._reject(**decision_request()))

    def test_a_healthy_channel_is_accepted(self) -> None:
        store = Mock()
        store.binding.return_value = dict(BINDING)
        with patch("control_plane.api.get_capability_store", return_value=store):
            asyncio.run(_validate_decision_generation_request(DecisionGenerationRequest(**decision_request())))


if __name__ == "__main__":
    unittest.main()
