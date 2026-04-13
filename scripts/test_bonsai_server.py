import os

from openai import OpenAI


def main() -> None:
    client = OpenAI(
        base_url=os.getenv("AUTON_LLM_BASE_URL", "http://localhost:8000/v1"),
        api_key=os.getenv("AUTON_LLM_API_KEY", "local"),
    )
    response = client.chat.completions.create(
        model=os.getenv("AUTON_LLM_MODEL", "bonsai-8b-v0.1.gguf"),
        temperature=0.3,
        messages=[
            {"role": "system", "content": "あなたは日本語で答えるアシスタントです。"},
            {"role": "user", "content": "こんにちは、自己紹介してください。"},
        ],
    )
    print(response.choices[0].message.content or "")


if __name__ == "__main__":
    main()
