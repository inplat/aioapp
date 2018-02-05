.PHONY: clean clean-test clean-pyc docs
.DEFAULT_GOAL := run
define BROWSER_PYSCRIPT
import os, webbrowser, sys
try:
	from urllib import pathname2url
except:
	from urllib.request import pathname2url

webbrowser.open("file://" + pathname2url(os.path.abspath(sys.argv[1])))
endef
export BROWSER_PYSCRIPT


VENV_EXISTS=$(shell [ -e .venv ] && echo 1 || echo 0 )
VENV_PATH=.venv
VENV_BIN=$(VENV_PATH)/bin
BROWSER := $(VENV_BIN)/python -c "$$BROWSER_PYSCRIPT"

.PHONY: clean
clean: clean-pyc clean-test clean-venv clean-install clean-mypy ## remove all build, test, coverage and Python artifacts

.PHONY: clean-pyc
clean-pyc: ## remove Python file artifacts
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +

.PHONY: clean-test
clean-test: ## remove test and coverage artifacts
	rm -f .coverage
	rm -fr htmlcov/
	rm -fr .tox/

.PHONY: clean-install
clean-install:
	find $(PACKAGES) -name '*.pyc' -delete
	find $(PACKAGES) -name '__pycache__' -delete
	rm -rf *.egg-info

.PHONY: clean-mypy
clean-mypy:
	rm -rf .mypy_cache

.PHONY: clean-venv
clean-venv:
	-rm -rf $(VENV_PATH)

.PHONY: venv
venv: ## Create virtualenv directory and install all requirements
ifeq ($(VENV_EXISTS), 1)
	@echo virtual env already initialized
else
	virtualenv -p python3.6 .venv
	$(VENV_BIN)/pip install Cython==0.27.3
	$(VENV_BIN)/pip install -r requirements_dev.txt
endif

.PHONY: flake8
flake8: venv ## flake8
	$(VENV_BIN)/flake8 aioapp examples tests setup.py

.PHONY: bandit
bandit: venv  # find common security issues in code
	$(VENV_BIN)/bandit -r ./aioapp ./examples setup.py

.PHONY: mypy
mypy: venv ## lint
	$(VENV_BIN)/mypy aioapp examples setup.py --ignore-missing-imports

.PHONY: lint
lint: flake8 bandit mypy ## lint

.PHONY: test
test: venv ## run tests
	$(VENV_BIN)/pytest

.PHONY: test-all
test-all: venv ## run tests on every Python version with tox
	$(VENV_BIN)/tox

.PHONY: fast-test-prepare
fast-test-prepare:
	-docker run -d --rm --name aioapp-test-tracer -p "10100:9411" -p "10101:16686" -e COLLECTOR_ZIPKIN_HTTP_PORT=9411 jaegertracing/all-in-one:latest
	-docker run -d --rm --name aioapp-test-rabbit -p "10102:5672" rabbitmq:latest
	-docker run -d --rm --name aioapp-test-postrges -p "10103:5432" postgres:latest
	-docker run -d --rm --name aioapp-test-redis -p "10104:6379" redis:latest


.PHONY: fast-test
fast-test:
	$(VENV_BIN)/pytest -s -v --rabbit-addr=127.0.0.1:10102 --postgres-addr=127.0.0.1:10103 --redis-addr=127.0.0.1:10104 --tracer-addr=127.0.0.1:10100 tests

.PHONY: coverage
fast-coverage: venv ## make coverage report and open it in browser
		$(VENV_BIN)/coverage run --source aioapp -m pytest tests -v --rabbit-addr=127.0.0.1:10102 --postgres-addr=127.0.0.1:10103 --redis-addr=127.0.0.1:10104 --tracer-addr=127.0.0.1:10100 tests
		$(VENV_BIN)/coverage report -m
		$(VENV_BIN)/coverage html
		$(BROWSER) htmlcov/index.html

.PHONY: fast-test-prepare
fast-test-stop:
	-docker stop aioapp-test-tracer
	-docker stop aioapp-test-postrges
	-docker stop aioapp-test-redis
	-docker stop aioapp-test-rabbit

.PHONY: coverage-quiet
coverage-quiet: venv ## make coverage report
		$(VENV_BIN)/coverage run --source aioapp -m pytest
		$(VENV_BIN)/coverage report -m
		$(VENV_BIN)/coverage html

.PHONY: coverage
coverage: venv coverage-quiet ## make coverage report and open it in browser
		$(BROWSER) htmlcov/index.html

.PHONY: docs
docs-quiet: venv ## make documentation
	rm -f docs/aioapp.rst
	rm -f docs/modules.rst
	.venv/bin/sphinx-apidoc -o docs/ aioapp
	$(MAKE) -C docs clean
	$(MAKE) -C docs html

.PHONY: docs
docs: venv docs-quiet ## make documentation and open it in browser
	$(BROWSER) docs/_build/html/index.html

.PHONY: servedocs
servedocs: docs ## compile the docs watching for changes
	$(VENV_BIN)/watchmedo shell-command -p '*.rst' -c '$(MAKE) -C docs html' -R -D .

.PHONY: release
release: venv lint test-all ## package and upload a release
	$(VENV_BIN)/python setup.py sdist upload
	$(VENV_BIN)/python setup.py bdist_wheel upload

.PHONY: dist
dist: clean venv ## builds source and wheel package
	$(VENV_BIN)/python setup.py sdist
	$(VENV_BIN)/python setup.py bdist_wheel
	ls -l dist

.PHONY: install
install: clean venv ## install the package to the active Python's site-packages
	$(VENV_BIN)/python setup.py install


.PHONY: help
help:  ## Show this help message and exit
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-23s\033[0m %s\n", $$1, $$2}'
