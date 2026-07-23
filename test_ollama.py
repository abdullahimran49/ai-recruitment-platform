"""Smoke test: prove the configured provider is reachable and returns valid
JSON. Run before using the app:

    python test_ollama.py
"""

import sys

import config
import llm


def main():
    print(f"Provider: {config.PROVIDER}  model: {config.MODEL}")
    ok, msg = llm.ping()
    print(msg)
    if not ok:
        sys.exit(1)

    print("\nAsking the model for a small JSON object...")
    result = llm.chat_json(
        system="You are a strict JSON generator. Output only JSON.",
        user='Return a JSON object with keys "skills" (list of 3 programming '
             'languages) and "count" (their number).',
    )
    print("Parsed response:", result)
    assert isinstance(result, dict) and "skills" in result
    print("\nOK — provider returns valid, parseable JSON.")


if __name__ == "__main__":
    main()
