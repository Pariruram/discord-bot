.PHONY: install dev test lint format run

install:
	pip install -r requirements.txt

dev:
	pip install -r requirements.txt -r requirements-dev.txt

test:
	python -m pytest -q

lint:
	python -m ruff check .

format:
	python -m ruff format .

run:
	python main.py
