# seshat Makefile
# Python-only build system with colorized output.
# The version lives in pyproject.toml; `make version` and the banner read it,
# so this header cannot drift out of date.

# ============== Colors & Symbols ==============
GREEN := \033[92m
EMERALD := \033[38;2;16;185;129m
CYAN := \033[96m
YELLOW := \033[93m
MAGENTA := \033[95m
RED := \033[91m
GRAY := \033[90m
BOLD := \033[1m
RESET := \033[0m

CHECK := ✓
CROSS := ✗
ARROW := ▸
PROGRESS := →

# ============== Boxes ==============
BOX_TOP := ╔══════════════════════════════════════════════════════════╗
BOX_BOT := ╚══════════════════════════════════════════════════════════╝

# ============== Project Metadata ==============
VERSION := $(shell grep -m1 '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
PYTHON_REQUIRED := 3.10+
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
UV := $(shell command -v uv 2>/dev/null)
CTGV := $(shell command -v commit-and-tag-version 2>/dev/null || printf 'npx commit-and-tag-version')
FILE_LIMIT := 500
RUNTIME_DIR := $(or $(XDG_RUNTIME_DIR),/run/user/$(shell id -u))
RUNTIME_ROOT := $(RUNTIME_DIR)/seshat

# ============== Phony Targets ==============
.PHONY: banner help check test test-one py-compile lint-files build ci-local pre-release \
        version bump-patch bump-minor bump-major bump-dry \
        doctor self-test integration runtime clean \
        _unit _py_compile _file_length

# ============== Default Target ==============
.DEFAULT_GOAL := help

# ============== Banner ==============
banner:
	@printf "$(EMERALD)$(BOLD)            _         _   \n"
	@printf " ___ ___ __| |_  __ _| |_ \n"
	@printf "(_-</ -_|_-< ' \\\\/ _\` |  _|\n"
	@printf "/__/\\\\___/__/_||_\\\\__,_|\\\\__|\n"
	@printf "                          $(RESET)\n"
	@printf "  $(GRAY)v$(VERSION)$(RESET) $(EMERALD)seshat — record a screen, narrate the take$(RESET)\n\n"

# ============== Gates ==============
# Undecorated bodies, so composing gates never re-print a banner.

_unit:
	@PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

_py_compile:
	@$(PYTHON) -m compileall -q src tests

_file_length:
	@$(PYTHON) scripts/check_file_length.py

test: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║                     Full Test Suite                      ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@printf "$(ARROW) $(BOLD)Running the unit suite...$(RESET)\n"
	@$(MAKE) _unit --no-print-directory && \
		printf "\n$(GREEN)$(CHECK) All tests passed$(RESET)\n\n" || \
		(printf "\n$(RED)$(CROSS) Tests failed$(RESET)\n\n" && exit 1)

test-one:
	@if [ -z "$(TEST)" ]; then \
		printf "$(CROSS) usage: $(BOLD)make test-one TEST=<pattern>$(RESET)\n"; \
		exit 1; \
	fi
	@printf "$(PROGRESS) Running tests matching: $(YELLOW)$(TEST)$(RESET)\n"
	@PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -k $(TEST) -v

py-compile:
	@printf "$(PROGRESS) Compiling sources...\n"
	@$(MAKE) _py_compile --no-print-directory && \
		printf "$(GREEN)$(CHECK) Bytecode compiled$(RESET)\n" || \
		(printf "$(RED)$(CROSS) Compilation failed$(RESET)\n" && exit 1)

lint-files:
	@printf "$(PROGRESS) Checking file lengths (< $(FILE_LIMIT) lines)...\n"
	@$(MAKE) _file_length --no-print-directory >/dev/null 2>&1 && \
		printf "$(GREEN)$(CHECK) File-length gate passed$(RESET)\n" || \
		($(PYTHON) scripts/check_file_length.py; exit 1)

check: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║                        Check Gate                        ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@printf "$(PROGRESS) Step 1/3: Unit suite...\n"
	@$(MAKE) _unit --no-print-directory >/dev/null 2>&1 && \
		printf "$(GREEN)$(CHECK) Unit suite passed$(RESET)\n" || \
		(printf "$(RED)$(CROSS) Unit suite failed — run $(BOLD)make test$(RESET)$(RED) for the output$(RESET)\n" && exit 1)
	@printf "$(PROGRESS) Step 2/3: Bytecode compile...\n"
	@$(MAKE) _py_compile --no-print-directory && \
		printf "$(GREEN)$(CHECK) Bytecode compiled$(RESET)\n" || \
		(printf "$(RED)$(CROSS) Compilation failed$(RESET)\n" && exit 1)
	@printf "$(PROGRESS) Step 3/3: File-length gate (< $(FILE_LIMIT) lines)...\n"
	@$(MAKE) _file_length --no-print-directory >/dev/null 2>&1 && \
		printf "$(GREEN)$(CHECK) File-length gate passed$(RESET)\n" || \
		($(PYTHON) scripts/check_file_length.py; exit 1)
	@printf "\n$(GREEN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(GREEN)$(BOLD)║                   $(CHECK) CHECK GATE PASSED                    ║$(RESET)\n"
	@printf "$(GREEN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"

# ============== Build ==============

build: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║                  Building Distributions                  ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@printf "$(ARROW) $(BOLD)Building sdist and wheel for v$(VERSION)...$(RESET)\n"
	@if [ -n "$(UV)" ]; then \
		$(UV) build >/dev/null 2>&1 && printf "$(GREEN)$(CHECK) Built with uv$(RESET)\n" || \
		(printf "$(RED)$(CROSS) uv build failed$(RESET)\n" && exit 1); \
	elif $(PYTHON) -c "import build" >/dev/null 2>&1; then \
		$(PYTHON) -m build >/dev/null 2>&1 && printf "$(GREEN)$(CHECK) Built with python -m build$(RESET)\n" || \
		(printf "$(RED)$(CROSS) python -m build failed$(RESET)\n" && exit 1); \
	else \
		printf "$(RED)$(CROSS) neither uv nor python -m build is available$(RESET)\n"; \
		printf "$(GRAY)  install the dev extra: uv pip install -e '.[dev]'$(RESET)\n\n"; \
		exit 1; \
	fi
	@printf "$(GRAY)  artifacts: %s$(RESET)\n\n" "$$(ls -1 dist 2>/dev/null | tr '\n' ' ')"

# ============== CI & Quality ==============

ci-local: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║                   Local CI Simulation                    ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@printf "$(ARROW) $(BOLD)Simulating the CI workflow locally...$(RESET)\n\n"
	@printf "$(PROGRESS) Step 1/4: Unit suite...\n"
	@$(MAKE) _unit --no-print-directory >/dev/null 2>&1 && \
		printf "$(GREEN)$(CHECK) Unit suite passed$(RESET)\n" || \
		(printf "$(RED)$(CROSS) Unit suite failed — run $(BOLD)make test$(RESET)$(RED) for the output$(RESET)\n" && exit 1)
	@printf "$(PROGRESS) Step 2/4: Bytecode compile...\n"
	@$(MAKE) _py_compile --no-print-directory && \
		printf "$(GREEN)$(CHECK) Bytecode compiled$(RESET)\n" || \
		(printf "$(RED)$(CROSS) Compilation failed$(RESET)\n" && exit 1)
	@printf "$(PROGRESS) Step 3/4: File-length gate...\n"
	@$(MAKE) _file_length --no-print-directory >/dev/null 2>&1 && \
		printf "$(GREEN)$(CHECK) File-length gate passed$(RESET)\n" || \
		($(PYTHON) scripts/check_file_length.py; exit 1)
	@printf "$(PROGRESS) Step 4/4: Distribution build...\n"
	@$(MAKE) build --no-print-directory >/dev/null 2>&1 && \
		printf "$(GREEN)$(CHECK) Distribution built$(RESET)\n" || \
		(printf "$(RED)$(CROSS) Distribution build failed — run $(BOLD)make build$(RESET)$(RED) for the output$(RESET)\n" && exit 1)
	@printf "\n$(GREEN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(GREEN)$(BOLD)║                  $(CHECK) CI SIMULATION PASSED                  ║$(RESET)\n"
	@printf "$(GREEN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"

pre-release: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║               Pre-Release Validation v$(VERSION)                ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@$(MAKE) check --no-print-directory
	@printf "$(PROGRESS) Building the exact tree that would be released...\n"
	@$(MAKE) build --no-print-directory >/dev/null 2>&1 && \
		printf "$(GREEN)$(CHECK) Distribution built$(RESET)\n\n" || \
		(printf "$(RED)$(CROSS) Distribution build failed — run $(BOLD)make build$(RESET)$(RED) for the output$(RESET)\n" && exit 1)
	@printf "$(GREEN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(GREEN)$(BOLD)║                   $(CHECK) READY FOR RELEASE                    ║$(RESET)\n"
	@printf "$(GREEN)$(BOLD)$(BOX_BOT)$(RESET)\n"
	@printf "$(GRAY)  next: make bump-minor   then: git push blackopsrepl main --follow-tags$(RESET)\n\n"

# ============== Version Management ==============
# commit-and-tag-version owns CHANGELOG.md and both version surfaces. No
# --no-verify: this repository has no hooks worth skipping.

version:
	@printf "$(CYAN)Current version:$(RESET) $(YELLOW)$(BOLD)$(VERSION)$(RESET)\n"

bump-patch: banner
	@printf "$(ARROW) Bumping patch version...\n"
	@$(CTGV) --release-as patch
	@printf "$(GREEN)$(CHECK) Version bumped$(RESET)\n"
	@printf "$(GRAY)  push: git push blackopsrepl main --follow-tags$(RESET)\n\n"

bump-minor: banner
	@printf "$(ARROW) Bumping minor version...\n"
	@$(CTGV) --release-as minor
	@printf "$(GREEN)$(CHECK) Version bumped$(RESET)\n"
	@printf "$(GRAY)  push: git push blackopsrepl main --follow-tags$(RESET)\n\n"

bump-major: banner
	@printf "$(ARROW) Bumping major version...\n"
	@$(CTGV) --release-as major
	@printf "$(GREEN)$(CHECK) Version bumped$(RESET)\n"
	@printf "$(GRAY)  push: git push blackopsrepl main --follow-tags$(RESET)\n\n"

bump-dry:
	@printf "$(PROGRESS) Previewing the next version bump...\n"
	@$(CTGV) --dry-run

# ============== Recording & Runtime ==============

doctor: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║                    Environment Doctor                    ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@PYTHONPATH=src $(PYTHON) -m seshat --doctor

self-test: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║                        Self-Test                         ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@PYTHONPATH=src $(PYTHON) -m seshat --self-test

integration: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║                Deadline Integration Check                ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@printf "$(RED)$(BOLD)WARNING: this records the live screen for a few seconds.$(RESET)\n"
	@if [ "$${SESHAT_INTEGRATION:-}" != "1" ]; then \
		printf "$(CROSS) refusing to run: opt in explicitly$(RESET)\n"; \
		printf "$(GRAY)  SESHAT_INTEGRATION=1 make integration$(RESET)\n"; \
		printf "$(GRAY)  SESHAT_INTEGRATION=1 SESHAT_INTEGRATION_SECONDS=20 make integration$(RESET)\n\n"; \
		exit 1; \
	fi
	@printf "\n"
	@PYTHONPATH=src $(PYTHON) scripts/integration_deadline.py

runtime: banner
	@printf "$(CYAN)$(BOLD)$(BOX_TOP)$(RESET)\n"
	@printf "$(CYAN)$(BOLD)║                      Runtime State                       ║$(RESET)\n"
	@printf "$(CYAN)$(BOLD)$(BOX_BOT)$(RESET)\n\n"
	@printf "$(ARROW) Runtime root: $(YELLOW)$(RUNTIME_ROOT)$(RESET)\n\n"
	@printf "$(PROGRESS) streams/ $(GRAY)(ingested for the next take)$(RESET)\n"
	@if [ -d "$(RUNTIME_ROOT)/streams" ]; then ls -1t $(RUNTIME_ROOT)/streams; else printf "$(GRAY)  (none yet)$(RESET)\n"; fi
	@printf "\n$(PROGRESS) recordings/ $(GRAY)(newest first, runtime-only)$(RESET)\n"
	@if [ -d "$(RUNTIME_ROOT)/recordings" ]; then ls -1t $(RUNTIME_ROOT)/recordings | head -10; else printf "$(GRAY)  (none yet)$(RESET)\n"; fi
	@printf "\n$(PROGRESS) active take: "
	@PYTHONPATH=src $(PYTHON) -c "from seshat import manager; print(manager.RECORDINGS.status())"
	@printf "\n"

# ============== Clean ==============

clean:
	@printf "$(ARROW) Cleaning build artifacts...\n"
	@rm -rf build dist *.egg-info src/*.egg-info
	@find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@printf "$(GREEN)$(CHECK) Clean complete$(RESET)\n"

# ============== Help ==============

help: banner
	@/bin/echo -e "$(CYAN)$(BOLD)Build:$(RESET)"
	@/bin/echo -e "  $(GREEN)make build$(RESET)          - Build sdist and wheel into dist/"
	@/bin/echo -e ""
	@/bin/echo -e "$(CYAN)$(BOLD)Test:$(RESET)"
	@/bin/echo -e "  $(GREEN)make test$(RESET)           - Run the full unit suite"
	@/bin/echo -e "  $(GREEN)make test-one TEST=name$(RESET) - Run tests matching a pattern, verbosely"
	@/bin/echo -e ""
	@/bin/echo -e "$(CYAN)$(BOLD)CI & Quality:$(RESET)"
	@/bin/echo -e "  $(GREEN)make check$(RESET)          - $(YELLOW)$(BOLD)The gate: unit suite + compile + file-length$(RESET)"
	@/bin/echo -e "  $(GREEN)make ci-local$(RESET)       - $(YELLOW)$(BOLD)Simulate the CI workflow locally$(RESET)"
	@/bin/echo -e "  $(GREEN)make pre-release$(RESET)    - Run every validation check before tagging"
	@/bin/echo -e "  $(GREEN)make py-compile$(RESET)     - Bytecode-compile src and tests"
	@/bin/echo -e "  $(GREEN)make lint-files$(RESET)     - Enforce the $(FILE_LIMIT)-line ceiling"
	@/bin/echo -e ""
	@/bin/echo -e "$(CYAN)$(BOLD)Version Management:$(RESET)"
	@/bin/echo -e "  $(GREEN)make version$(RESET)        - Show the current version"
	@/bin/echo -e "  $(GREEN)make bump-patch$(RESET)     - Bump patch version (0.1.$(YELLOW)x$(RESET))"
	@/bin/echo -e "  $(GREEN)make bump-minor$(RESET)     - Bump minor version (0.$(YELLOW)x$(RESET).0)"
	@/bin/echo -e "  $(GREEN)make bump-major$(RESET)     - Bump major version ($(YELLOW)x$(RESET).0.0)"
	@/bin/echo -e "  $(GREEN)make bump-dry$(RESET)       - Preview the next bump"
	@/bin/echo -e ""
	@/bin/echo -e "$(CYAN)$(BOLD)Recording & Runtime:$(RESET)"
	@/bin/echo -e "  $(GREEN)make doctor$(RESET)         - What this host can record and narrate with"
	@/bin/echo -e "  $(GREEN)make self-test$(RESET)      - Non-mutating session checks"
	@/bin/echo -e "  $(GREEN)make runtime$(RESET)        - Streams, recordings, and the active take"
	@/bin/echo -e "  $(GREEN)make integration$(RESET)    - $(RED)$(BOLD)Records the live screen$(RESET) — needs SESHAT_INTEGRATION=1"
	@/bin/echo -e ""
	@/bin/echo -e "$(CYAN)$(BOLD)Other:$(RESET)"
	@/bin/echo -e "  $(GREEN)make clean$(RESET)          - Remove build output and caches"
	@/bin/echo -e "  $(GREEN)make help$(RESET)           - Show this help message"
	@/bin/echo -e ""
	@/bin/echo -e "$(GRAY)Python required: $(PYTHON_REQUIRED)   Runtime root: $(RUNTIME_ROOT)$(RESET)"
	@/bin/echo -e "$(GRAY)Current version: v$(VERSION)$(RESET)"
	@/bin/echo -e ""
