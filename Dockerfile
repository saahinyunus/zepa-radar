# Apify Store container for the APIFY_ACTOR execution mode.
FROM apify/actor-python:3.12

ENV EXECUTION_MODE=APIFY_ACTOR \
    PYTHONUNBUFFERED=1

# Install dependencies first so Docker caches this layer between code changes.
# (The Actor does not need FastAPI/Uvicorn, so requirements-apify.txt is used.)
COPY requirements-apify.txt requirements-cron.txt ./
RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir -r requirements-apify.txt \
    && echo "All installed Python packages:" \
    && pip freeze

COPY . ./

# Fail the build early on syntax errors.
RUN python3 -m compileall -q src apify_main.py

CMD ["python3", "apify_main.py"]
