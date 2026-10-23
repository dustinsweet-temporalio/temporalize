"""The coding agent pauses at its budget, saves its conversation, and a later call carries on from there."""
from types import SimpleNamespace

from temporalize.generate import agent


class Block(SimpleNamespace):
    def model_dump(self, **_):
        return {k: v for k, v in vars(self).items() if v is not None}


class FakeLLM:
    """Writes one file per turn, then calls finish on the fourth turn."""

    def __init__(self):
        self.seen: list[int] = []   # how many messages each call was sent

    def create(self, **params):
        self.seen.append(len(params["messages"]))
        n = len(self.seen)
        if n == 4:
            call = Block(type="tool_use", id=f"t{n}", name="finish", input={"summary": "built it"})
        else:
            call = Block(type="tool_use", id=f"t{n}", name="write_file", input={"path": f"f{n}.txt", "content": "x"})
        return SimpleNamespace(content=[Block(type="thinking", thinking="…", signature="sig"), call],
                               stop_reason="tool_use")


def test_the_agent_resumes_its_saved_conversation(tmp_path, monkeypatch):
    ws = tmp_path / "r-0001"
    ws.mkdir()
    monkeypatch.setattr(agent, "poc_env", lambda _ws: {})
    llm = FakeLLM()
    first = agent.run_agent(llm, ws, "system", "task", lambda _m: None, max_turns=10, session="generate", turn_budget=2)
    assert not first.done and first.turns == 2 and not first.finished
    assert (agent.sessions_dir(ws) / "generate.json").exists()

    second = agent.run_agent(llm, ws, "system", "task", lambda _m: None,
                             max_turns=10, session="generate", turn_budget=2)
    assert second.done and second.finished and second.turns == 4 and second.summary == "built it"
    assert llm.seen == [1, 3, 5, 7]          # the third call was sent the whole saved conversation
    assert sorted(p.name for p in ws.iterdir()) == ["f1.txt", "f2.txt", "f3.txt"]

    agent.reset_sessions(ws)
    assert not agent.sessions_dir(ws).exists()
