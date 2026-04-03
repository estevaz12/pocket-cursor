# Convenience targets (CI uses npm run validate).
.PHONY: setup validate dev

setup:
	python3 -m pip install -r requirements.txt -r requirements-dev.txt
	npm install
	cd tests && npm ci

validate:
	npm run validate

# One-liner for local iteration: install deps then run the bridge (requires .env + Cursor CDP).
dev: setup
	@echo "Then run: python -X utf8 pocket_cursor.py"
