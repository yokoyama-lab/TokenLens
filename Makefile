PYTHON ?= python3
APP := src/tokenlens.py
STATUS_ARGS ?=
DOCTOR_ARGS ?=
PROVIDERS_ARGS ?=
CONFIG_ARGS ?=
INSTALL_ARGS ?=

.PHONY: help install editable uninstall status doctor providers config test check clean

help:
	@echo "Targets:"
	@echo "  make status [STATUS_ARGS='<provider> --json']"
	@echo "  make doctor [DOCTOR_ARGS='<provider> --json']"
	@echo "  make providers [PROVIDERS_ARGS='--json']"
	@echo "  make config [CONFIG_ARGS='show']"
	@echo "  make install [INSTALL_ARGS='--user']"
	@echo "  make editable"
	@echo "  make uninstall"
	@echo "  make test"
	@echo "  make check"
	@echo "  make clean"

install:
	$(PYTHON) -m pip install $(INSTALL_ARGS) .

editable:
	$(PYTHON) -m pip install -e .

uninstall:
	$(PYTHON) -m pip uninstall -y tokenlens

status:
	$(PYTHON) $(APP) status $(STATUS_ARGS)

doctor:
	$(PYTHON) $(APP) doctor $(DOCTOR_ARGS)

providers:
	$(PYTHON) $(APP) providers $(PROVIDERS_ARGS)

config:
	$(PYTHON) $(APP) config $(CONFIG_ARGS)

test:
	$(PYTHON) -m unittest discover -s tests -v

check:
	$(PYTHON) -m py_compile $(APP)
	$(PYTHON) -m unittest discover -s tests -v

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
