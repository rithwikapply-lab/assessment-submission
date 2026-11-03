from llm_router.tokens import estimate_tokens, estimate_request_tokens

def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2

def test_estimate_request_tokens():
    messages = [
        {"role": "user", "content": "hello"}, # len 5 -> ceil(5/4) = 2 + 4 = 6
        {"role": "assistant", "content": "world!"} # len 6 -> ceil(6/4) = 2 + 4 = 6
    ]
    # total = 6 + 6 + 100 = 112
    assert estimate_request_tokens(messages, 100) == 112
