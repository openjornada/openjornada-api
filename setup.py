# OpenJornada - Sistema de Registro de Jornada Laboral
# Copyright (C) 2024 HappyAndroids (https://happyandroids.com)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from setuptools import setup, find_packages

setup(
    name="openjornada-api",
    version="1.0.0",
    description="OpenJornada API - Sistema de Registro de Jornada Laboral",
    long_description=open("../README.md").read() if __name__ == "__main__" else "",
    long_description_content_type="text/markdown",
    author="HappyAndroids",
    author_email="info@openjornada.es",
    url="https://www.openjornada.es",
    license="AGPL-3.0",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "fastapi>=0.104.0",
        "motor>=3.3.2",
        "pydantic>=2.5.0",
        "pydantic-settings>=2.1.0",
        "passlib[bcrypt]>=1.7.4",
        "python-jose[cryptography]>=3.3.0",
        "python-multipart>=0.0.6",
        "uvicorn[standard]>=0.24.0",
        "httpx>=0.25.2",
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Framework :: FastAPI",
        "Topic :: Office/Business",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
    ],
    keywords="time-tracking workforce-management agpl open-source openjornada",
    project_urls={
        "Homepage": "https://www.openjornada.es",
        "Bug Reports": "https://github.com/openjornada/openjornada-api/issues",
        "Source": "https://github.com/openjornada/openjornada-api",
        "Documentation": "https://github.com/openjornada/openjornada-api/tree/main/docs",
        "Author": "https://happyandroids.com",
    },
)
