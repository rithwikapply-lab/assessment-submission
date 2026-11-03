import math
from typing import List, Dict, Any

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / 4)

def estimate_request_tokens(messages: List[Dict[str, Any]], max_tokens: int) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        total += estimate_tokens(content) + 4
    total += max_tokens
    return total
