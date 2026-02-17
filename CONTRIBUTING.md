# Contributing to OpenJornada API

Thank you for your interest in contributing to the OpenJornada API component!

> **Note**: This is part of the [OpenJornada Project](https://bitbucket.org/[YOUR-WORKSPACE]/projects/OPENJORNADA).
> For general project information and documentation, see [openjornada-core](https://bitbucket.org/[YOUR-WORKSPACE]/openjornada-core).

## 📋 Quick Links

- [Main Project Documentation](https://bitbucket.org/[YOUR-WORKSPACE]/openjornada-core/src/main/docs/)
- [General Contributing Guidelines](https://bitbucket.org/[YOUR-WORKSPACE]/openjornada-core/src/main/CONTRIBUTING.md)
- [API Documentation](https://bitbucket.org/[YOUR-WORKSPACE]/openjornada-core/src/main/docs/API.md)

## 🚀 API Development Setup

### Prerequisites

- Python 3.11+
- MongoDB 6.0+
- Docker (optional, recommended)

### Local Setup

```bash
# Clone this repository
git clone git@bitbucket.org:[YOUR-WORKSPACE]/openjornada-api.git
cd openjornada-api

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Edit .env with your configuration

# Run the API
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

### With Docker

```bash
docker build -t openjornada-api .
docker run -p 8000:8000 --env-file .env openjornada-api
```

## 📝 Coding Standards for Python/FastAPI

### Style Guide

- **Follow PEP 8**: Python style guide
- **Use type hints**: All functions should have type annotations
- **Docstrings**: Use Google-style docstrings for all public functions
- **Formatting**: Run `black` before committing
- **Linting**: Run `flake8` and fix all warnings

```bash
# Format code
black api/

# Check linting
flake8 api/

# Type checking
mypy api/
```

### Code Structure

```python
# Good example with proper structure
from typing import Optional, List
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Depends

router = APIRouter()

class UserRequest(BaseModel):
    """Request model for creating a user.

    Attributes:
        email: User's email address
        name: User's full name
    """
    email: str = Field(..., description="User email")
    name: str = Field(..., min_length=1, max_length=100)

@router.post("/users", response_model=UserResponse)
async def create_user(
    request: UserRequest,
    current_user: User = Depends(get_current_user)
) -> UserResponse:
    """Create a new user.

    Args:
        request: User creation request
        current_user: Currently authenticated user

    Returns:
        Created user data

    Raises:
        HTTPException: If user already exists
    """
    # Implementation here
    pass
```

### Testing

All new features and bug fixes must include tests:

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=api --cov-report=html

# Run specific test
pytest tests/test_time_records.py -v
```

### API Endpoint Guidelines

1. **Use proper HTTP methods**:
   - GET: Retrieve data
   - POST: Create resources
   - PUT/PATCH: Update resources
   - DELETE: Delete resources

2. **Use proper status codes**:
   - 200: Success
   - 201: Created
   - 400: Bad request
   - 401: Unauthorized
   - 403: Forbidden
   - 404: Not found
   - 500: Server error

3. **Consistent naming**:
   - Use plural nouns: `/api/users`, `/api/time-records`
   - Use kebab-case for URLs
   - Use snake_case for JSON fields

4. **Error handling**:
```python
from fastapi import HTTPException

# Good error handling
if not user:
    raise HTTPException(
        status_code=404,
        detail="User not found"
    )
```

## 🔄 Pull Request Process

1. Create a feature branch: `git checkout -b feature/add-new-endpoint`
2. Make your changes following the coding standards
3. Add tests for your changes
4. Update API documentation if needed
5. Run tests and linting locally
6. Commit with clear messages: `feat: add endpoint for pause statistics`
7. Push to your fork: `git push origin feature/add-new-endpoint`
8. Create a Pull Request in Bitbucket
9. Link relevant Jira issues (e.g., "OT-123")

### PR Checklist

- [ ] Code follows PEP 8 and project standards
- [ ] Type hints are used for all functions
- [ ] Tests are added and passing
- [ ] Documentation is updated
- [ ] No linting errors (`flake8`)
- [ ] Code is formatted (`black`)
- [ ] AGPL-3.0 license headers in new files
- [ ] Bitbucket Pipeline passes

## 🧪 Testing Guidelines

### Unit Tests

```python
# tests/test_time_records.py
import pytest
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)

def test_create_time_record():
    """Test creating a time record."""
    response = client.post(
        "/api/time-records/",
        json={
            "email": "worker@example.com",
            "password": "password123",
            "company_id": "507f1f77bcf86cd799439011",
            "action": "entry"
        }
    )
    assert response.status_code == 201
    assert response.json()["type"] == "entry"
```

### Integration Tests

Test complete workflows, including database operations.

## 📖 Documentation

When adding or modifying endpoints:

1. Update docstrings in the code
2. Update API documentation in `openjornada-core/docs/API.md`
3. Add examples in docstrings

## ⚖️ License Agreement

By contributing to OpenJornada API, you agree that:

1. Your contributions will be licensed under **AGPL-3.0**
2. You have the right to contribute the code
3. All new source files must include the license header:

```python
# OpenJornada - Sistema de Registro de Jornada Laboral
# Copyright (C) 2024 HappyAndroids (https://happyandroids.com)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
```

## 🐛 Debugging

### View API logs
```bash
docker-compose logs -f api
```

### Access MongoDB
```bash
docker-compose exec mongodb mongosh
use openjornada
db.time_records.find().limit(10)
```

### Debug with VS Code

Add this to `.vscode/launch.json`:
```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "FastAPI",
            "type": "python",
            "request": "launch",
            "module": "uvicorn",
            "args": [
                "api.main:app",
                "--reload",
                "--host", "0.0.0.0",
                "--port", "8000"
            ],
            "jinja": true
        }
    ]
}
```

## 💬 Communication

- **Bitbucket Issues**: Bug reports and feature requests
- **Pull Requests**: Code review and discussions
- **Jira**: Task tracking (link issues with OT-XXX)

## 📚 Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Pydantic Documentation](https://docs.pydantic.dev/)
- [Motor (MongoDB async) Documentation](https://motor.readthedocs.io/)
- [Python Type Hints](https://docs.python.org/3/library/typing.html)

---

Thank you for contributing to OpenJornada API! 🚀

For questions specific to this component, open an issue in this repository.
For general project questions, see [openjornada-core](https://bitbucket.org/[YOUR-WORKSPACE]/openjornada-core).
