# Starts and stops the per-user Actual Budget containers through the Docker socket.
# Standard library only, so there are no dependencies to install or keep patched.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY provisioner.py .

EXPOSE 8090

FROM base AS gateway
EXPOSE 8091
CMD ["python", "-c", "from provisioner import gateway_main; gateway_main()"]

FROM base AS relay
# The relay has no socket, host data mount, or root privileges.
USER 65534:65534
CMD ["python", "provisioner.py"]
