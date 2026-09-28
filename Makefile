PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: check test py-compile lint-files doctor integration build clean

check: test py-compile lint-files

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

py-compile:
	$(PYTHON) -m compileall -q src tests

lint-files:
	$(PYTHON) scripts/check_file_length.py

doctor:
	PYTHONPATH=src $(PYTHON) -m seshat --doctor

# Records the live screen; opt in explicitly. Never part of check.
integration:
	PYTHONPATH=src $(PYTHON) scripts/integration_deadline.py

build:
	$(PYTHON) -m build

clean:
	rm -rf build dist *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
