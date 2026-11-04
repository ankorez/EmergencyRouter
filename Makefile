.PHONY: test lint check image integration ui-test

test:   ## tests unitaires
	python3 -B -m unittest discover -s tests -v

lint:   ## analyse statique Python + shellcheck
	python3 -m pyflakes erouter tests
	shellcheck -S warning -e SC2154 install.sh build-image.sh image/stage-erouter/prerun.sh image/stage-erouter/00-install/00-run.sh image/postprocess.sh scripts/erouter-fs scripts/erouter-data scripts/update-blocklist.sh tests/integration/check.sh tests/integration/firewall.sh

check: lint test

image:  ## construit l'image flashable (Docker requis)
	./build-image.sh

integration:  ## vérifications dans un vrai Debian 13 (Docker, conteneur privilégié)
	docker run --rm --privileged -v "$(CURDIR)":/src:ro debian:trixie bash /src/tests/integration/check.sh

ui-test:  ## interface dans un vrai Chrome (sans écran) contre un faux serveur
	python3 tests/ui/smoke.py
