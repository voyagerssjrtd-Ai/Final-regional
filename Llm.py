from langchain_openai import ChatOpenAI
import os
import httpx
from typing import Any, Dict

# Disable SSL verification for demo purposes (not recommended for production)
client = httpx.Client(verify=False)

def get_llm_client() -> ChatOpenAI:
    """Create and return a ChatOpenAI client. The API key is read from the GENAILAB_API_KEY
    environment variable; if not set a placeholder is used (replace in production).
    """
    api_key = os.environ.get("GENAILAB_API_KEY", "sk-1911M1pwvPABMH5FjVjt4A")
    return ChatOpenAI(
        base_url="https://genailab.tcs.in",
        model="azure_ai/genailab-maas-DeepSeek-V3-0324",
        api_key="sk-1911M1pwvPABMH5FjVjt4A",
        http_client=client
    )

def call_llm(prompt: str) -> Dict[str, Any]:
    """Invoke the LLM with the provided prompt and return a JSON-serializable dict.
    Returns {'text': ...} on success or {'error': ...} on failure.
    """
    llm = get_llm_client()
    try:
        resp = llm.invoke(prompt)
        # Convert response to a simple serializable form. The ChatOpenAI return type
        # may be an object; coerce to string for safety.
        return {"text": str(resp)}
    except Exception as e:
        return {"error": str(e)}
