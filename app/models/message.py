# Backward-compat shim — superseded by conversation.py
from app.models.conversation import (  # noqa: F401
    ChatMessage,
    Conversation,
    ConversationParticipant,
)

# Legacy aliases
Message = ChatMessage
