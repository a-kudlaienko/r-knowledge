.PHONY: guide clean build install-local

PACKAGE_NAME := repo-knowledge
REPO_PATH = readlink -f .

clean:
	rm -rfv dist build *.egg-info src/*.egg-info

build: clean
	python -m pip install --upgrade build
	python -m build

install-local: build
	python -m pip install --no-index --find-links=dist $(PACKAGE_NAME)

guide:
	@echo ""
	@echo " * To build and install ${PACKAGE_NAME} inside your project Python venv:"
	@echo "     - jump in inside your repo (not this one)"
	@echo "     - create VENV if doesn't exist: 'python -m venv ~/.venv'"
	@echo "     - activate your Python venv: 'source ~/.venv/bin/activate'"
	@echo "     - python -m pip install -e `${REPO_PATH}`"
	@echo ""
	@echo ""
	@echo " * Build the knowledge base:"
	@echo "     - knowledge build"
	@echo "   First time: scan + chunk + embed (cold: 1-5 min)"
	@echo ""
	@echo ""
	@echo " * Then check how it works(EXAMPLE):"
	@echo "     - knowledge search 'terraform resource: load balancer' --kind resource --lang hcl"
	@echo ""
	@echo ""
	@echo " * To update the knowledge base:"
	@echo "     - knowledge update"
	@echo "   Incremental; auto-detects changed files"
