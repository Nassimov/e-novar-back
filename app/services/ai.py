from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

import anthropic

from app.config import get_settings

settings = get_settings()

_client: Optional[anthropic.Anthropic] = None

MODEL = "claude-sonnet-4-6"


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def explain_concept(subject: str, question: str, level: str) -> str:
    """Explain an educational concept with Claude. Returns full text response."""
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": (
                    f"You are an expert Algerian tutor teaching {subject} to a {level} student.\n"
                    f"Explain the following in clear, simple Arabic or French (match the language of the question):\n\n"
                    f"{question}\n\n"
                    "Use examples relevant to the Algerian curriculum. Be concise and pedagogical."
                ),
            }
        ],
    )
    return message.content[0].text


def explain_concept_stream(subject: str, question: str, level: str):
    """Stream an explanation from Claude."""
    client = get_client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": (
                    f"You are an expert Algerian tutor teaching {subject} to a {level} student.\n"
                    f"Explain the following in clear, simple Arabic or French (match the question language):\n\n"
                    f"{question}\n\n"
                    "Use examples relevant to the Algerian curriculum."
                ),
            }
        ],
    ) as stream:
        for text in stream.text_stream:
            yield text


def generate_homework_hint(homework_data: Dict[str, Any]) -> str:
    """Generate a targeted hint for a homework problem without giving away the full answer."""
    client = get_client()
    subject = homework_data.get("subject", "")
    title = homework_data.get("title", "")
    statement = homework_data.get("statement", "")
    level = homework_data.get("level", "")

    message = client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[
            {
                "role": "user",
                "content": (
                    f"You are tutoring a {level} student on {subject}.\n"
                    f"Homework: {title}\n"
                    f"Problem: {statement}\n\n"
                    "Give ONE helpful hint that guides the student towards the solution "
                    "WITHOUT revealing the answer. Keep it brief and encouraging."
                ),
            }
        ],
    )
    return message.content[0].text


def solve_homework(homework_data: Dict[str, Any]) -> str:
    """Provide a full solution with step-by-step explanation."""
    client = get_client()
    subject = homework_data.get("subject", "")
    title = homework_data.get("title", "")
    statement = homework_data.get("statement", "")
    level = homework_data.get("level", "")

    message = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Solve this {subject} homework for a {level} student.\n"
                    f"Title: {title}\n"
                    f"Problem: {statement}\n\n"
                    "Provide a complete, step-by-step solution with clear explanations. "
                    "Show all work and explain each step. Use the same language as the problem statement."
                ),
            }
        ],
    )
    return message.content[0].text


def generate_quiz(subject: str, level: str, num_questions: int = 5) -> List[Dict[str, Any]]:
    """Generate multiple-choice quiz questions. Returns list of question dicts."""
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Generate {num_questions} multiple-choice questions for {subject} at {level} level "
                    f"following the Algerian school curriculum.\n\n"
                    "Return ONLY valid JSON array with this structure:\n"
                    '[\n  {\n    "question": "...",\n    "options": ["A", "B", "C", "D"],\n'
                    '    "correct_index": 0,\n    "explanation": "..."\n  }\n]\n\n'
                    "No extra text, just the JSON array."
                ),
            }
        ],
    )
    text = message.content[0].text.strip()
    # Extract JSON from the response
    start = text.find("[")
    end = text.rfind("]") + 1
    if start != -1 and end > start:
        text = text[start:end]
    return json.loads(text)


def evaluate_quiz_answer(question: str, user_answer: str, correct_answer: str) -> Dict[str, Any]:
    """Evaluate a quiz answer and return score + explanation."""
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n"
                    f"Correct answer: {correct_answer}\n"
                    f"Student answer: {user_answer}\n\n"
                    "Is the student's answer correct? Return JSON: "
                    '{"is_correct": true/false, "score": 0-100, "explanation": "..."}'
                ),
            }
        ],
    )
    text = message.content[0].text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]
    return json.loads(text)


def generate_progress_report(student_data: Dict[str, Any]) -> str:
    """Generate a markdown progress report for a student."""
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": (
                    "Generate a detailed student progress report in Markdown format based on this data:\n\n"
                    f"{json.dumps(student_data, ensure_ascii=False, indent=2)}\n\n"
                    "Include sections: Summary, Strengths, Areas for Improvement, Recommendations. "
                    "Be constructive and encouraging. Write in French."
                ),
            }
        ],
    )
    return message.content[0].text


def generate_session_summary(session_data: Dict[str, Any]) -> str:
    """Generate a markdown summary of a tutoring session."""
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {
                "role": "user",
                "content": (
                    "Generate a concise session summary in Markdown based on this data:\n\n"
                    f"{json.dumps(session_data, ensure_ascii=False, indent=2)}\n\n"
                    "Include: Topics covered, Key points, Homework assigned, Next steps. "
                    "Keep it professional and brief. Write in French."
                ),
            }
        ],
    )
    return message.content[0].text
