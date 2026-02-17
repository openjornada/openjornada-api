FROM python:3.10-slim

WORKDIR /app

# Install MongoDB database tools (for mongodump/mongorestore)
# Using tarball for cross-architecture support (ARM64 and x86_64)
# Version 100.9.0 - tested and working on both architectures
RUN apt-get update && apt-get install -y wget ca-certificates libgssapi-krb5-2 && \
    ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then \
        MONGO_TOOLS_URL="https://fastdl.mongodb.org/tools/db/mongodb-database-tools-ubuntu2004-arm64-100.9.0.tgz"; \
    elif [ "$ARCH" = "amd64" ]; then \
        MONGO_TOOLS_URL="https://fastdl.mongodb.org/tools/db/mongodb-database-tools-ubuntu2004-x86_64-100.9.0.tgz"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    wget -O /tmp/mongodb-tools.tgz "$MONGO_TOOLS_URL" && \
    tar -xzf /tmp/mongodb-tools.tgz -C /tmp && \
    cp /tmp/mongodb-database-tools-*/bin/* /usr/local/bin/ && \
    rm -rf /tmp/mongodb-tools.tgz /tmp/mongodb-database-tools-* && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY ./api /app/api

# Create backups directory (for local storage option)
RUN mkdir -p /app/backups && chmod 755 /app/backups

# Run the application
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
