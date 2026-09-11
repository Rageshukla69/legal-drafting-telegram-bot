"""Azure OpenAI adapter.

Requires the official `openai` Python package and environment variables:
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_API_KEY
AZURE_OPENAI_API_VERSION
AZURE_OPENAI_DEPLOYMENT

This module is intentionally small: application logic remains provider-neutral.
"""
from __future__ import annotations
import json, os

def draft_with_azure(prompt_package: dict, schema: dict):
    try:
        from openai import AzureOpenAI
    except ImportError as e:
        raise RuntimeError("Install the `openai` package before using Azure.") from e

    required = [
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
    ]
    missing = [x for x in required if not os.getenv(x)]
    if missing:
        raise RuntimeError("Missing Azure environment variables: " + ", ".join(missing))

    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
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
            {"role": "user", "content": json.dumps(prompt_package, ensure_ascii=False)},
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
