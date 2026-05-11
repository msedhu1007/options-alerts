FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir requests anthropic sendgrid twilio python-dotenv
CMD ["python", "options_agent.py"]
