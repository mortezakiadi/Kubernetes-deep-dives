# One image for every platform service. Each Deployment picks its program with a command.
# Built for linux/amd64 (the platform nodes) and pushed to the ECR repository Terraform creates.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app

# Pinned: the agent-sandbox SDK (router), and mitmproxy (egress gateway).
RUN pip install "k8s-agent-sandbox[grpc]==1.0.10" "mitmproxy==12.3.1"

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin platform
WORKDIR /app
COPY ds01 /app/ds01
USER 10001
