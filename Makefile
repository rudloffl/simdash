# simdash: AMS2 live dashboard + session recorder (FastAPI)
PREFIX ?= $(HOME)/.local
# simapi's include dir (only needed for `make offsets`)
SIMAPI_INC ?= ../monocoque/src/monocoque/simulatorapi/simapi/include
DIR := $(abspath .)

venv: .venv/bin/uvicorn
.venv/bin/uvicorn: requirements.txt
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt

# Regenerate field offsets if simapi's pcars2data.h changes
offsets:
	$(CC) -std=gnu11 -I$(SIMAPI_INC) -o gen_offsets gen_offsets.c
	./gen_offsets > pcars2_offsets.json
	rm -f gen_offsets

# Installs the launcher only. To record at every login (optional):
#   install -Dm644 simdash.service ~/.config/systemd/user/simdash.service
#   systemctl --user daemon-reload && systemctl --user enable --now simdash
install: venv
	printf '#!/bin/sh\ncd "%s" || exit 1\nexec .venv/bin/python -m simdash "$$@"\n' "$(DIR)" > $(PREFIX)/bin/simdash
	chmod 755 $(PREFIX)/bin/simdash

.PHONY: venv offsets install
