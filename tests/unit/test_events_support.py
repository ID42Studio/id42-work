from _factories import T0
from id42_work import EventDispatcher, EventType, WorkEvent
from id42_work.events import REDACTED, default_redactor


def test_default_redactor_masks_sensitive_keys_recursively() -> None:
    data = {
        "password": "p",
        "Authorization": "Bearer x",
        "nested": {"api_key": "k", "count": 2, "items": [{"session_cookie": "c"}, 3]},
        "safe": "value",
    }
    assert default_redactor(data) == {
        "password": REDACTED,
        "Authorization": REDACTED,
        "nested": {"api_key": REDACTED, "count": 2, "items": [{"session_cookie": REDACTED}, 3]},
        "safe": "value",
    }


def test_dispatcher_delivers_in_subscription_order() -> None:
    dispatcher = EventDispatcher()
    seen: list[str] = []
    dispatcher.subscribe(lambda e: seen.append(f"a:{e.sequence_number}"))
    dispatcher.subscribe(lambda e: seen.append(f"b:{e.sequence_number}"))
    dispatcher.publish(
        WorkEvent(
            event_type=EventType.EXECUTION_CREATED,
            execution_id=None,
            sequence_number=1,
            timestamp=T0,
        )
    )
    assert seen == ["a:1", "b:1"]
