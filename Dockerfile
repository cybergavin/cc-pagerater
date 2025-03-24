# Define UID and GID to be used as non-root user in final image
ARG UID="65000"
ARG GID="65000"

# Stage 1: Builder
FROM python:3.13.2-alpine3.21@sha256:323a717dc4a010fee21e3f1aac738ee10bb485de4e7593ce242b36ee48d6b352 AS builder

# Set the working directory
WORKDIR /app

# Install build dependencies
RUN apk add --no-cache build-base libpq-dev postgresql-dev libffi-dev \
    openssl-dev musl-dev gcc g++ make

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps -r requirements.txt -w /wheels

# Stage 2: Final Image
FROM python:3.13.2-alpine3.21@sha256:323a717dc4a010fee21e3f1aac738ee10bb485de4e7593ce242b36ee48d6b352 

# Declare ARG variables
ARG UID
ARG GID

# Use environment variables at runtime
ENV UID=${UID} GID=${GID}

# Create non-root user and group
RUN addgroup -g ${GID} appgroup && \
    adduser -u ${UID} -G appgroup -S appuser

# Set up the application directory and adjust permissions
RUN mkdir -p /app && chown appuser:appgroup /app

# Switch to the non-root user
USER appuser

# Set the working directory
WORKDIR /app

# Copy wheels and install dependencies
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*

# Add application code
COPY --chown=appuser:appgroup templates /app/templates
COPY --chown=appuser:appgroup cc_pagerater*.py /app/

# Health check to verify if the application is listening on port 8000
HEALTHCHECK CMD ["wget", "-q", "-O", "/tmp/healthcheck.json", "http://0.0.0.0:8000/health"]

# Run the application
CMD ["python", "cc_pagerater_api.py"]