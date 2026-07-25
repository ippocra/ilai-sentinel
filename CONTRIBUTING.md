# Contributing to ilai-sentinel

Thank you for your interest in contributing! This document covers the basics.

## Getting Started

```bash
# Clone the repo
git clone https://github.com/ippocra/ilai-sentinel.git
cd ilai-sentinel

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies (including dev deps)
pip install -e ".[dev]"

# Run linting
ruff check src/

# Run tests
pytest tests/
```

## Code Style

- We use **ruff** for linting and formatting
- Line length: 100 characters
- Python 3.11+ only

## Pull Requests

1. Fork the repo and create a feature branch
2. Make your changes with tests
3. Ensure `ruff check src/` passes cleanly
4. Submit a PR with a clear description of what and why

## Reporting Issues

- Use the GitHub issue tracker
- Include: what you expected, what actually happened, steps to reproduce
