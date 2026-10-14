# dsi-monsoon-2026-05a
Supporting facilitators: How might we help facilitators recognize what is happening in a sensitive group conversation and decide when and how intervention may be useful?

## Repository Structure

- `app.py`: Backend Flask service for session management, conversation preparation, plan generation, and evaluation.
- `gemini.py`: Gemini client integrating planning, clarification, tone analysis, and audio reflection summaries.
- `requirements.txt`: Python dependencies for the backend service.
- `tests/`: Test suite including backend endpoint tests and Gemini integration tests.
  - `tests/test_backend.py`: Unit tests for session lifecycle, validation, and API routes.
  - `tests/test_gemini.py`: Unit tests for Gemini prompt construction and response parsing.
- `data/hackathon1/`: Curated conversation datasets, annotations, export utilities, and validation scripts.

## Running Tests

Run backend unit tests:
```bash
python3 -m unittest tests.test_backend
```
