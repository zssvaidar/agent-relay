"""Quick check that your Anthropic API key works.

Usage:
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python test.py
"""
import sys

import anthropic


def main() -> int:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    try:
        response = client.messages.create(
            model="claude-opus-5-5",
            max_tokens=1024,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Say hello and tell me 2 + 2 in one short sentence."}],
        )
    except anthropic.AuthenticationError:
        print("FAIL: invalid API key (401).")
        return 1
    except anthropic.PermissionDeniedError:
        print("FAIL: key lacks permission for this model (403).")
        return 1
    except anthropic.RateLimitError as e:
        print(f"FAIL: rate limited or out of credits (429): {e.message}")
        print(f"retry-after: {e.response.headers.get('retry-after')}")
        return 1
    except anthropic.APIStatusError as e:
        print(f"FAIL: API error {e.status_code}: {e.message}")
        return 1
    except anthropic.APIConnectionError:
        print("FAIL: could not reach the API (network error).")
        return 1

    if response.stop_reason == "refusal":
        print("Key works, but the request was refused.")
        return 0

    text = "".join(b.text for b in response.content if b.type == "text")
    print("OK: API key works!")
    print(f"Model:    {response.model}")
    print(f"Reply:    {text}")
    print(f"Tokens:   {response.usage.input_tokens} in / {response.usage.output_tokens} out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
