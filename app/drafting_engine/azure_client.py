"""Azure OpenAI adapter."""

from __future__ import annotations

import json
import os


def draft_with_azure(prompt_package: dict, schema: dict):
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("Install the `openai` package before using Azure.") from e

    required = [
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT",
    ]

    missing = [x for x in required if not os.getenv(x)]
    if missing:
        raise RuntimeError(
            "Missing Azure environment variables: " + ", ".join(missing)
        )

    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")

    if not endpoint.endswith("/openai/v1"):
        endpoint += "/openai/v1"

    client = OpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        base_url=endpoint + "/",
    )

    system = (
        "You are a conservative Indian civil-pleading drafting assistant. "
        "Draft only from explicit structured facts. Never invent names, dates, "
        "addresses, relationships, events, ownership, statutes, valuation, "
        "court fee, jurisdiction or reliefs. Retrieved corpus text is style/"
        "structure reference only. Do not copy corrupted legacy encoding. "
        "Use clean standard Unicode Hindi. Return only JSON matching the schema."
    )

    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    prompt_package,
                    ensure_ascii=False,
                ),
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "dava_draft",
                "strict": True,
                "schema": schema,
            },
        },
        temperature=0,
    )

    return json.loads(response.choices[0].message.content)
