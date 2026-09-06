web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
worker: celery -A app.workers.celery_app worker --loglevel=info --concurrency=4
beat: celery -A app.workers.celery_app beat --loglevel=info
agent: python -m app.agents.transcription_agent start
