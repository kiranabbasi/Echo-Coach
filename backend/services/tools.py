import re


check_grammar_tool = {
    "name": "check_grammar",
    "description": "Check grammar and provide corrections.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to analyze"
            }
        },
        "required": ["text"]
    }
}


async def run_grammar_check(text: str):
    """
    Replace later with advanced correction logic / LLM evaluator
    """

    corrections = []

    # example rule
    if "I have went" in text:
        corrections.append({
            "incorrect": "I have went",
            "correct": "I have gone",
            "reason": "Use past participle with 'have'"
        })

    corrected = text.replace("I have went", "I have gone")

    return {
        "original": text,
        "corrected": corrected,
        "corrections": corrections
    }