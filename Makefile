.PHONY: dev-db netd api web web-build guacd test lint migrate netd-hostb-setup netd-hostb iso iso-test handbook docs-site release install

export LABTRIS_DATABASE_URL ?= postgresql+asyncpg://pnl:pnl@localhost/pnl
export LABTRIS_NETD_SOCKET ?= /run/labtris/netd.sock

dev-db:
	docker compose -f docker-compose.dev.yml up -d
	@echo "waiting for postgres..."
	@until docker compose -f docker-compose.dev.yml exec -T postgres pg_isready -U pnl -d pnl >/dev/null 2>&1; do sleep 1; done
	alembic upgrade head

migrate:
	alembic upgrade head

netd:
	mkdir -p /run/labtris
	python3 -m labtris_netd --socket $(LABTRIS_NETD_SOCKET)

api:
	uvicorn labtris_api.main:app --reload --host 0.0.0.0 --port 8080

web:
	cd web && npm install && npm run dev -- --host 0.0.0.0 --port 5173

# `web/dist` is gitignored, and labtris_api.main only mounts it when it exists —
# rebuild after any UI edit if you're serving the SPA from the API on :8080
# rather than from the vite dev server.
web-build:
	cd web && npm install && npm run build

# The VNC/RDP consoles tunnel to this. Debian/Ubuntu: `apt install guacd`
# (add `libguac-client-rdp0` for RDP).
guacd:
	guacd -f -L info

test:
	pytest -q
	cd web && npm test


lint:
	ruff check labtris_api labtris_netd tests
	mypy labtris_api labtris_netd

# Multi-host demo: simulates a second host as its own network namespace,
# reachable from this one over a veth "underlay" link — see docs/04-scaling.md.
# Register it with `POST /hosts {"endpoint": "tcp://10.201.0.2:9601", "token": "labtris-demo-token-b", "underlay_ip": "10.201.0.2"}`.
netd-hostb-setup:
	sudo ip netns add labtris-hostb 2>/dev/null || true
	sudo ip link add veth-hosta type veth peer name veth-hostb 2>/dev/null || true
	sudo ip link set veth-hostb netns labtris-hostb 2>/dev/null || true
	sudo ip addr add 10.201.0.1/30 dev veth-hosta 2>/dev/null || true
	sudo ip link set veth-hosta up
	sudo ip netns exec labtris-hostb ip addr add 10.201.0.2/30 dev veth-hostb 2>/dev/null || true
	sudo ip netns exec labtris-hostb ip link set veth-hostb up
	sudo ip netns exec labtris-hostb ip link set lo up

netd-hostb: netd-hostb-setup
	sudo ip netns exec labtris-hostb python3 -m labtris_netd \
		--socket /run/labtris/netd-hostb.sock \
		--tcp-listen 10.201.0.2:9601 \
		--token labtris-demo-token-b

# ---------------------------------------------------------------- packaging
# Build the installer ISO. Caches Ubuntu's base image under .cache/iso, so the
# first run downloads ~3 GB and later ones do not.
#   make iso                       latest pinned release
#   make iso RELEASE=24.04.3       a specific point release
iso:
	./packaging/iso/build.sh

# The handbook as one PDF, for the release page and for people who want it
# offline. Renders with headless Chrome; no LaTeX, no pandoc.
handbook:
	python3 docs/build-pdf.py

# docs.labtris.com. Stdlib only, reusing build-pdf.py's renderer, so the
# PDF and the site cannot disagree about how a page renders.
docs-site:
	python3 docs/build-site.py

# Snapshot the OpenAPI spec into docs/api/openapi.json so the Mintlify docs
# site can render REST reference pages from it. Reads from an already-running
# instance because generating it in-process needs the full dependency set
# installed; on a dev box `make api` is running anyway. Override the URL:
#   make openapi API_URL=http://<host>:8080
API_URL ?= http://127.0.0.1:8080
openapi:
	curl -fsSL $(API_URL)/openapi.json | python3 -m json.tool > docs/api/openapi.json
	@echo "wrote docs/api/openapi.json ($$(wc -c < docs/api/openapi.json) bytes)"

# Build the ISO and the handbook, then tag and publish. --dry-run builds and
# checksums without publishing anything. Must run on Ubuntu: the ISO's package
# closure is resolved against the target's own release.
release:
	./packaging/release.sh

# Boot the ISO under QEMU against a scratch disk. Nothing on this machine is
# touched; the installed UI is forwarded to http://localhost:8443.
iso-test:
	./packaging/iso/test-boot.sh $(firstword $(wildcard dist/labtris-*.iso))

# Install onto THIS machine from the working tree. The same script the ISO
# runs, so testing it here tests the ISO's install path too.
install:
	sudo ./packaging/install-labtris.sh --source $(CURDIR)
