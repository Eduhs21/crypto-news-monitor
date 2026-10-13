"""Small REST client for the Gemini API (plain ``requests``).

It asks explicitly for JSON output and parses defensively, because LLMs sometimes wrap
the JSON in ```json ... ``` fences even when told not to.
"""
import json
import requests

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _close_truncated_json(text: str) -> str:
    """Close objects, lists and strings cut off mid-response.

    Covers the two ways ``maxOutputTokens`` truncates a response: the model stops right
    after a complete value (only the closing delimiters are missing), or it stops in the
    MIDDLE of a string, e.g. "reasoning" grew too long and was cut mid-sentence. In the
    second case the open quote is closed and only the tail of the sentence is lost,
    instead of discarding the whole answer.
    """
    stack = []
    in_string = False
    escaped = False
    pairs = {"{": "}", "[": "]"}

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(char)
        elif char in ("}", "]"):
            if not stack or pairs[stack[-1]] != char:
                return text
            stack.pop()

    trimmed = text.rstrip()
    if in_string:
        if escaped:  # ends on a dangling backslash: drop it so it does not escape the closing quote
            trimmed = trimmed[:-1]
        trimmed += '"'
    elif trimmed.endswith(","):
        trimmed = trimmed[:-1]

    return trimmed + "".join(pairs[opening] for opening in reversed(stack))


def parse_json_response(text: str) -> dict:
    """Parse the model response into JSON, repairing missing trailing delimiters."""
    text_clean = text.strip()
    if text_clean.startswith("```"):
        import re
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text_clean = match.group(1)

    try:
        return json.loads(text_clean)
    except json.JSONDecodeError as original_error:
        # Repair truncated JSON (missing closing delimiters, e.g. cut by maxOutputTokens)
        repaired = _close_truncated_json(text_clean)
        if repaired != text_clean:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

        # Sometimes the model writes complete JSON and then appends extra text
        # (a comment, a repetition): keep only the first valid JSON value.
        try:
            value, _ = json.JSONDecoder().raw_decode(text_clean)
            return value
        except json.JSONDecodeError:
            pass

        raise RuntimeError(
            f"Model did not return valid JSON: {text_clean[:300]}"
        ) from original_error


def build_prompt(portfolio_data: list, watchlist_data: list) -> str:
    """Build the prompt with every collected data point (prices + headlines)."""

    def format_coin_block(coin):
        news_block = "\n".join(f'  - "{h}"' for h in coin.get("headlines", [])) or "  (no recent headlines)"
        return f"""
Moeda: {coin['symbol']}
Current price: ${coin['price']:.4f}
Change 24h: {coin['change_24h']:.2f}%
Change 7d: {coin.get('change_7d', 0):.2f}%
Manchetes recentes:
{news_block}
"""

    portfolio_block = "\n".join(format_coin_block(c) for c in portfolio_data) or "No coins in the portfolio."
    watchlist_block = "\n".join(format_coin_block(c) for c in watchlist_data) or "No coins in the watchlist."

    return f"""You are a crypto market analyst. Analyse the data below (prices + recent headlines)
and answer STRICTLY in valid JSON, with no markdown and no text outside the JSON, in this shape:

{{
  "opportunities": [
    {{"symbol": "XXX", "signal": "buy" | "neutral", "confidence": 0.0-1.0, "reasoning": "1-2 sentences"}}
  ],
  "risks": [
    {{"symbol": "XXX", "risk_level": "high" | "medium" | "low", "confidence": 0.0-1.0, "reasoning": "1-2 sentences"}}
  ]
}}

Rules:
- "opportunities" evaluates ONLY the WATCHLIST coins (possible buy).
- "risks" evaluates ONLY the PORTFOLIO coins (downside risk, given the user buy price).
- Be conservative: high confidence (>0.7) only when price AND headlines point the same way.
- Never invent headlines that were not supplied.

=== PORTFOLIO (coins already bought) ===
{portfolio_block}

=== WATCHLIST (coins of interest for a buy) ===
{watchlist_block}
"""


def call_gemini(prompt: str, api_key: str, model: str = "gemini-3.5-flash",
                timeout: int = 60, max_retries: int = 3, max_output_tokens: int = 3072) -> dict:
    """Call the Gemini API and return the parsed JSON response.

    Retries up to *max_retries* times with exponential backoff on 429/503 or a network
    error (timeout/connection). ``max_output_tokens`` caps the response size; the prompts
    already ask for short answers (1-3 sentences), so this is a safety net against an
    over-long response rather than a real constraint.
    """
    import time

    url = GEMINI_ENDPOINT.format(model=model)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "maxOutputTokens": max_output_tokens,
        },
    }

    resp = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries:
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                print(f"[Gemini] Network error ({type(e).__name__}), retrying in {wait}s... ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            # Never use str(e): it can contain the full request URL, API key included.
            raise RuntimeError("Gemini: network failure after several retries (timeout or connection).") from None

        if resp.status_code in (429, 503) and attempt < max_retries:
            wait = 2 ** attempt * 5  # 5s, 10s, 20s
            print(f"[Gemini] Rate limited (HTTP {resp.status_code}), retrying in {wait}s... ({attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue
        if not resp.ok:
            # requests puts the full URL (API key included) in its default exception.
            raise RuntimeError(
                f"Gemini respondeu HTTP {resp.status_code}. "
                "Check the quota, the configured model and the API key."
            )
        break

    data = resp.json()

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected response from the Gemini API: {data}") from e

    return parse_json_response(text)
