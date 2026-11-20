from agentlab.audit import AuditStore
from agentlab.domain import Stage, Visibility


def test_audit_hash_chain_and_redaction(tmp_path):
    store = AuditStore(tmp_path / "run", "r1")
    store.append(Stage.API_REQUESTED, Visibility.EVALUATOR_PRIVATE, {"api_key": "secret", "text": "contains sk-abcdefghijklmnop"})
    store.append(Stage.ACTION_EXECUTED, Visibility.AGENT_VISIBLE, {"ok": True})
    valid, problem = store.verify()
    assert valid and problem is None
    text = store.events_path.read_text()
    assert "abcdefghijklmnop" not in text and '"api_key": "[REDACTED]"' in text

