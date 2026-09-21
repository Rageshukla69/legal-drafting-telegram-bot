#!/usr/bin/env bash
# Simulate the Heroku PDF pipeline end to end with Docker:
#
#   1. run the real heroku-community/apt buildpack against this repository's
#      Aptfile inside the official *build* image (heroku/heroku:<stack>-build);
#   2. run the relocated LibreOffice inside the official *run* image
#      (heroku/heroku:<stack>) — which lacks the build image's extra libraries —
#      with the exact command line and environment the converter uses;
#   3. verify the produced PDFs glyph by glyph on the host.
#
# This is what CI runs (see .github/workflows/ci.yml); locally:
#
#   scripts/heroku_dyno_simulation.sh 24        # heroku-24 (Ubuntu 24.04)
#   scripts/heroku_dyno_simulation.sh 22        # heroku-22 (Ubuntu 22.04)
#
# Requirements on the host: docker, git, python with requirements-dev.txt.
set -euo pipefail

STACK="${1:-24}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${HEROKU_SIM_WORK:-/tmp/heroku-sim-${STACK}}"
OUT="${HEROKU_SIM_OUT:-${ROOT}/tests/output/heroku-sim-${STACK}}"
SLUG="${WORK}/slug"
CACHE="${WORK}/cache"
BUILDPACK="${WORK}/apt-buildpack"

BUILD_IMAGE="heroku/heroku:${STACK}-build"
RUN_IMAGE="heroku/heroku:${STACK}"

rm -rf "${WORK}"
mkdir -p "${SLUG}" "${CACHE}" "${WORK}/env" "${OUT}"

echo "==> [1/4] apt buildpack against Aptfile inside ${BUILD_IMAGE}"
git clone --quiet --depth 1 https://github.com/heroku/heroku-buildpack-apt.git "${BUILDPACK}"
cp "${ROOT}/Aptfile" "${SLUG}/Aptfile"
chmod -R a+rwX "${WORK}"
docker run --rm \
  -v "${SLUG}:/tmp/slug" -v "${CACHE}:/tmp/cache" -v "${WORK}/env:/tmp/env" -v "${BUILDPACK}:/tmp/apt-buildpack" \
  -e "STACK=heroku-${STACK}" \
  "${BUILD_IMAGE}" \
  bash -c "/tmp/apt-buildpack/bin/compile /tmp/slug /tmp/cache /tmp/env" | tail -n 40

echo "==> apt payload size (added to the slug):"
APT_SIZE="$(du -sh "${SLUG}/.apt" | cut -f1)"
echo "    ${APT_SIZE}"
ls -1 "${CACHE}/apt/cache/archives/"*.deb | xargs -n1 basename | sort > "${OUT}/packages-heroku-${STACK}.txt"
PKG_COUNT="$(wc -l < "${OUT}/packages-heroku-${STACK}.txt")"
echo "    ${PKG_COUNT} .deb packages (list: ${OUT}/packages-heroku-${STACK}.txt)"
annotate() { # level title message
  if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::$1 title=$2::$3"; fi
}
annotate notice "heroku-${STACK} apt buildpack" "${PKG_COUNT} packages unpacked into .apt (${APT_SIZE})"
test -e "${SLUG}/.apt/usr/bin/soffice" || { annotate error "heroku-${STACK}" "soffice was not unpacked to .apt/usr/bin"; echo "!! soffice was not unpacked to .apt/usr/bin"; exit 1; }

echo "==> [2/4] staging the Hindi test DOCX, pinned fonts and the exact soffice command"
python "${ROOT}/scripts/pdf_devanagari_font_test.py" --write-dyno-script "${SLUG}/sim" --dyno-prefix /app/sim --dyno-app-dir /app
chmod -R a+rwX "${SLUG}"

echo "==> [3/4] converting inside ${RUN_IMAGE} (no build-image libraries available)"
# The run image has no Python, so the conversion is driven by the generated
# shell script; HOME=/app mirrors a dyno. The default user of the run image is
# used deliberately (a dyno does not run as root).
LOG="${OUT}/conversion-heroku-${STACK}.log"
set +e
docker run --rm -v "${SLUG}:/app" -w /app -e HOME=/app "${RUN_IMAGE}" bash /app/sim/run_conversion.sh > "${LOG}" 2>&1
STATUS=$?
set -e
cat "${LOG}"
VERSION_LINE="$(grep -m1 -i "^LibreOffice" "${LOG}" || true)"
MISSING="$(grep "not found" "${LOG}" | sed 's/ =>.*//' | awk '{print $NF}' | sort -u | tr '\n' ' ' || true)"
if [ -n "${MISSING// /}" ]; then
  annotate warning "heroku-${STACK} missing shared libraries on the run image" "${MISSING}"
fi
if [ "${STATUS}" -ne 0 ]; then
  annotate error "heroku-${STACK} conversion failed" "exit ${STATUS}: $(tail -n 3 "${LOG}" | tr '\n' ' ' | cut -c1-600)"
  exit "${STATUS}"
fi
annotate notice "heroku-${STACK} run image" "${VERSION_LINE:-soffice started} — converted the Hindi test DOCX with the relocated LibreOffice"

cp "${SLUG}"/sim/out/*.pdf "${OUT}/" 2>/dev/null || { annotate error "heroku-${STACK}" "no PDF produced inside the run image"; echo "!! no PDF produced inside the run image"; exit 1; }

echo "==> [4/4] glyph-level inspection of the dyno-produced PDFs"
python "${ROOT}/scripts/pdf_devanagari_font_test.py" --inspect-pdf "${OUT}"/devanagari_font_test.pdf "${OUT}"/devanagari_mixed_font.pdf --out "${OUT}" ${GITHUB_ACTIONS:+--github-annotations}
echo "Heroku-${STACK} dyno simulation PASSED; artifacts in ${OUT}"
