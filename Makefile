.PHONY: test lint check image

test:   ## tests unitaires
	python3 -B -m unittest discover -s tests -v

lint:   ## analyse statique Python + shellcheck
	python3 -m pyflakes erouter tests
	shellcheck -e SC2154 install.sh build-image.sh image/stage-erouter/prerun.sh image/stage-erouter/00-install/00-run.sh scripts/erouter-fs

check: lint test

image:  ## construit l'image flashable (Docker requis)
	./build-image.sh
