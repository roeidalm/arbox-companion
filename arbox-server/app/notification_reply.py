"""An in-place reply to one actionable notification, on its source channel."""
from dataclasses import dataclass, field


@dataclass
class NotificationReply:
    text: str
    buttons: list[list[dict]] = field(default_factory=list)
    tag: str | None = None
